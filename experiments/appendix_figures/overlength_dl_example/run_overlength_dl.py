#!/usr/bin/env python3
"""
Appendix D (fig:dl_anim2 / fig:dl_scc2) regeneration: the *constraint-induced*
multi-train overlength deadlock example.

This is the SECOND App D example. As learned constraints accumulate over
iterations on seed 1 (the same N=10, phi=0.25 overlength scenario as the first
example), the deadlock SCC inflates beyond the core overlength pair {T46,T49}
by pulling in bystander trains via constraint-induced (C) edges. The
full-backer-set pruning fix (exe build 2026-06-02) drops bystanders that are
co-backed and held by no *physical* edge, so the SCC that the paper described as
four trains now collapses to three.

Two modes:

  python run_overlength_dl.py                 # DISCOVERY
      START_DEBUG=0 => dump a conflict_graph_<iter>.dot at every deadlock
      iteration, run MAX_ITER iterations on seed 1, and print a per-iteration
      table (constraint_details + pruning_details) so we can pick the iteration
      whose SCC matches the App D example. DOTs are kept in ./dots/.

  python run_overlength_dl.py --capture K     # CAPTURE iteration K
      Re-run with START_DEBUG=K, MAX_ITER=K+1 and animation ON, so iteration K
      is the only one that animates -> a clean anim_script.json + a single
      conflict_graph_K.dot for that exact deadlock. Artifacts copied to ./out/.

Uses the bundle's current exe (full-backer fix) and the exact
configs_overlength/N10 config, matching results/overlength/N10/f0.25.
"""
import argparse
import csv
import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, CAMPAIGN_DIR)
sys.path.insert(0, os.path.join(CAMPAIGN_DIR, "runners"))
from run_overlength_exp import (  # noqa: E402
    set_csv_option, run_exe,
    read_base_spawners, write_overlength_spawners,
)
from run_overlength import ensure_base_config, base_config_dir  # noqa: E402

# Match the fullbacker overlength N10/f0.25 scenario exactly.
N = 10
FRACTION = 0.25
RATE = 2.00
SEED = 1
DISCOVERY_MAX_ITER = 40   # original example was at iteration ~32

WORKDIR = os.path.join(SCRIPT_DIR, "work")
DOTS_DIR = os.path.join(SCRIPT_DIR, "dots")
OUT_DIR = os.path.join(SCRIPT_DIR, "out")


def setup():
    ensure_base_config(N)
    input_dir = os.path.join(WORKDIR, "input")
    output_dir = os.path.join(WORKDIR, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    base = os.path.join(base_config_dir(N), "input")
    for fn in os.listdir(base):
        s = os.path.join(base, fn)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(input_dir, fn))

    # PL-only learning: strip any engineered nogood_constr.csv.
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)

    write_overlength_spawners(
        os.path.join(input_dir, "open_loop_spawners.csv"),
        RATE, FRACTION, read_base_spawners(base_config_dir(N)))

    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp, "WARM_START_FILE", "")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(runctrl, "seed", SEED)
    return input_dir, output_dir


def raw_rows(path):
    """Read dlexp_log.csv, skipping the malformed final cap row (empty
    iteration), keeping ALL columns (incl. constraint_details/pruning_details)."""
    with open(path, newline="") as fh:
        rd = csv.DictReader(fh)
        rows = [r for r in rd if (r.get("iteration") or "").strip().isdigit()]
    return rows


def discovery():
    input_dir, output_dir = setup()
    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(dlexp, "MAX_ITERATIONS", DISCOVERY_MAX_ITER)
    set_csv_option(dlexp, "START_DEBUG", 0)        # DOT at every deadlock iter
    set_csv_option(runctrl, "animate", 0)          # no animation in discovery
    # clear any stale dot/log
    for fn in os.listdir(output_dir):
        if fn.endswith(".dot"):
            os.remove(os.path.join(output_dir, fn))

    print(f"DISCOVERY: N={N}, phi={FRACTION}, rate={RATE}, seed={SEED}, "
          f"MAX_ITER={DISCOVERY_MAX_ITER}, START_DEBUG=0")
    run_exe(WORKDIR)

    log = os.path.join(output_dir, "dlexp_log.csv")
    rows = raw_rows(log)

    # stash DOTs
    os.makedirs(DOTS_DIR, exist_ok=True)
    for fn in os.listdir(DOTS_DIR):
        os.remove(os.path.join(DOTS_DIR, fn))
    for fn in os.listdir(output_dir):
        if fn.endswith(".dot"):
            shutil.copy2(os.path.join(output_dir, fn),
                         os.path.join(DOTS_DIR, fn))

    print(f"\n{'iter':>4} {'dl?':>3} {'t':>5} {'+c':>4} {'tot':>5}  "
          f"{'constraint_details':28} pruning_details")
    print("-" * 90)
    for r in rows:
        it = r["iteration"]
        dl = r["deadlock_found"]
        t = float(r["deadlock_time"])
        nc = r["new_constraints"]
        tot = r["total_constraints"]
        cd = (r.get("constraint_details") or "")[:28]
        pd = (r.get("pruning_details") or "")
        flag = ""
        # flag a multi-train SCC that pruning collapsed (orig>final, orig>=3)
        if pd and ">" in pd:
            try:
                orig = int(pd.split(">")[0].lstrip("0123456789"[:0]).strip()
                           or pd.split(">")[0])
            except ValueError:
                orig = 0
            if orig >= 3:
                flag = "  <== multi-train collapse"
        print(f"{it:>4} {dl:>3} {t:>5.1f} {nc:>4} {tot:>5}  {cd:28} {pd}{flag}")

    print(f"\nDOTs saved in {DOTS_DIR}")
    print("Inspect a candidate with:  grep -E 'subgraph|->|label' "
          f"{DOTS_DIR}/conflict_graph_<iter>.dot")
    print("Then capture:  python run_overlength_dl.py --capture <iter>")


def apply_colored_templates(input_dir):
    """Rewrite rs/train templates + remap spawners for direction+length colour:
    loco=red, EB=blue / WB=green, short=light / overlength=dark. Colour comes
    from the sprite file (the exe applies no tint), so we point each car class at
    a dedicated coloured sprite (generated in Animation/)."""
    rs = os.path.join(input_dir, "rs_templates.csv")
    with open(rs, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "length", "sprite_file", "sprite_w", "sprite_h", "sprite_scale"])
        w.writerow(["loco",         0.02, "train_red.png",     50, 100, 0.2])
        w.writerow(["car_eb_short", 0.02, "car_eb_short.png",  25, 50,  0.1])
        w.writerow(["car_eb_long",  0.02, "car_eb_long.png",   25, 50,  0.1])
        w.writerow(["car_wb_short", 0.02, "car_wb_short.png",  25, 50,  0.1])
        w.writerow(["car_wb_long",  0.02, "car_wb_long.png",   25, 50,  0.1])

    tt = os.path.join(input_dir, "train_templates.csv")
    hdr = ["name", "max_spd", "accel", "decel",
           "set0_n", "set0_rst", "set1_n", "set1_rst",
           "set2_n", "set2_rst", "set3_n", "set3_rst", "set4_n", "set4_rst"]
    rows = [
        ["freight_EB",      80, 360, 720, 2, "loco", 20, "car_eb_short", "", "", "", "", "", ""],
        ["freight_long_EB", 80, 360, 720, 2, "loco", 38, "car_eb_long",  "", "", "", "", "", ""],
        ["freight_WB",      80, 360, 720, 2, "loco", 20, "car_wb_short", "", "", "", "", "", ""],
        ["freight_long_WB", 80, 360, 720, 2, "loco", 38, "car_wb_long",  "", "", "", "", "", ""],
    ]
    with open(tt, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        w.writerows(rows)

    # Remap each spawner's train_template (col 3) by its name.
    spawners = os.path.join(input_dir, "open_loop_spawners.csv")
    with open(spawners, newline="") as fh:
        rows = list(csv.reader(fh))
    for r in rows[1:]:
        if not r:
            continue
        name = r[0]
        long = "long" in name
        wb = "WB" in name
        r[3] = ("freight_long_" if long else "freight_") + ("WB" if wb else "EB")
    with open(spawners, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)


def capture(k):
    input_dir, output_dir = setup()
    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(dlexp, "MAX_ITERATIONS", k + 1)
    set_csv_option(dlexp, "START_DEBUG", k)        # only iteration k debugs
    set_csv_option(runctrl, "animate", 1)          # animate iteration k
    # render_cars is not in the base runctrl (exe pre-scans it, default off);
    # append it so the animation emits full consists (to-scale trains).
    with open(runctrl, newline="") as fh:
        has_rc = any(r and r[0].strip() == "render_cars" for r in csv.reader(fh))
    if not has_rc:
        with open(runctrl, "a", newline="") as fh:
            csv.writer(fh).writerow(["render_cars", "1", ""])
    else:
        set_csv_option(runctrl, "render_cars", 1)

    apply_colored_templates(input_dir)

    # exe writes anim_script.json to ../Animation relative to cwd (=WORKDIR)
    anim_dir = os.path.join(WORKDIR, "..", "Animation")
    os.makedirs(anim_dir, exist_ok=True)

    print(f"CAPTURE iteration {k}: START_DEBUG={k}, MAX_ITER={k+1}, animate=1")
    run_exe(WORKDIR)

    os.makedirs(OUT_DIR, exist_ok=True)
    dot = os.path.join(output_dir, f"conflict_graph_{k}.dot")
    if os.path.exists(dot):
        shutil.copy2(dot, os.path.join(OUT_DIR, f"conflict_graph_{k}.dot"))
        print(f"  -> {OUT_DIR}/conflict_graph_{k}.dot")
    anim = os.path.normpath(os.path.join(anim_dir, "anim_script.json"))
    if os.path.exists(anim):
        shutil.copy2(anim, os.path.join(OUT_DIR, f"anim_iter{k}.json"))
        print(f"  -> {OUT_DIR}/anim_iter{k}.json")
    log = os.path.join(output_dir, "dlexp_log.csv")
    for r in raw_rows(log):
        if int(r["iteration"]) == k:
            print(f"  iter {k}: dl={r['deadlock_found']} t={r['deadlock_time']} "
                  f"+{r['new_constraints']}c total={r['total_constraints']} "
                  f"details={r.get('constraint_details')} "
                  f"pruning={r.get('pruning_details')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", type=int, default=None,
                    help="iteration to capture (animation + single DOT)")
    args = ap.parse_args()
    if args.capture is not None:
        capture(args.capture)
    else:
        discovery()


if __name__ == "__main__":
    main()
