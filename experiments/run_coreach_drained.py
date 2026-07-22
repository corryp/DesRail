#!/usr/bin/env python3
"""Phase C: coreachability no-op check on ALL drained-safe seeds.

For every seed the drained held-out certified truly safe (unprotected deadlock-
free AND drained-clear), the per-N union supervisor must be a byte-identical
no-op over the stated window [0, horizon]. This is the strong coreachability-
preservation test: it runs on the FULL safe set (all rates), not a sample.

Consumes the safe-seed lists written by run_heldout_drained.py
(results/heldout_drained/safe_seeds/N{NN}_r{rate}.txt). Each (N,rate) cell is
sliced into DRAIN_SLICES contiguous chunks so each PBS job finishes inside
walltime; a merge step aggregates the per-slice counts.

Usage:
  python3 run_coreach_drained.py cell --N 5 --rate 2.00 --slice 0
  python3 run_coreach_drained.py merge --N 5 --rate 2.00
  python3 run_coreach_drained.py prove --jobs 8            # N02/N05 r1/r2, all slices
"""
import argparse, os, sys, csv, hashlib, shutil, math
from concurrent.futures import ProcessPoolExecutor, as_completed
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import run_heldout_eval as H
import run_heldout_drained as D
from run_experiments import (set_csv_option, set_spawn_rate, run_exe,
                             rate_label, scenario_label, apply_drain)

SLICES = 10
CO_WORK = os.path.join(SCRIPT_DIR, "work", "coreach_drained")
CO_RESULTS = os.path.join(SCRIPT_DIR, "results", "coreach_drained")


def safe_list_path(N, rate):
    return os.path.join(D.SAFE_DIR, f"N{N:02d}_{rate_label(rate)}.txt")


def load_safe(N, rate):
    p = safe_list_path(N, rate)
    if not os.path.exists(p):
        return []
    return [int(x) for x in open(p).read().split()]


def cell_results_dir(N, rate):
    return os.path.join(CO_RESULTS, f"N{N:02d}", rate_label(rate))


def part_path(N, rate, sidx):
    return os.path.join(cell_results_dir(N, rate), f"noop.part_{sidx:02d}.csv")


def slice_seeds(N, rate, sidx):
    safe = load_safe(N, rate)
    if not safe:
        return []
    chunk = math.ceil(len(safe) / SLICES)
    return safe[sidx * chunk:(sidx + 1) * chunk]


def setup(N, rate, sidx):
    """Isolated drained work dir with the per-N union at the root."""
    wdir = os.path.join(CO_WORK, f"N{N:02d}", rate_label(rate), f"slice_{sidx:02d}")
    idir = os.path.join(wdir, "input")
    os.makedirs(idir, exist_ok=True)
    os.makedirs(os.path.join(wdir, "output"), exist_ok=True)
    base = os.path.join(H.CONFIGS_DIR, f"N{N:02d}", "input")
    for f in os.listdir(base):
        p = os.path.join(base, f)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(idir, f))
    set_spawn_rate(os.path.join(idir, "open_loop_spawners.csv"), rate)
    upath = H.union_path(N)
    if not os.path.exists(upath):
        H.build_union_constraints(N)
    shutil.copy2(upath, os.path.join(wdir, "union.csv"))
    d = os.path.join(idir, "dlexp_options.csv"); r = os.path.join(idir, "runctrl.csv")
    set_csv_option(d, "MAX_ITERATIONS", 1); set_csv_option(d, "CONSTRAINT_EVAL", "tree")
    set_csv_option(d, "START_DEBUG", 0)
    set_csv_option(r, "pl_constraints", 0); set_csv_option(r, "log_output", 2)
    apply_drain(r)                      # same drained horizon as training/held-out
    return wdir


def _md5_window(tl_path, horizon):
    """md5 of train_log rows with time <= horizon (the certified window)."""
    if not os.path.exists(tl_path):
        return None
    h = hashlib.md5()
    with open(tl_path, "rb") as f:
        for line in f:
            try:
                if float(line.split(b",", 1)[0]) > horizon:
                    continue
            except ValueError:
                pass
            h.update(line)
    return h.hexdigest()


def _run(wdir, seed, warm, horizon):
    idir = os.path.join(wdir, "input")
    set_csv_option(os.path.join(idir, "runctrl.csv"), "seed", seed)
    set_csv_option(os.path.join(idir, "dlexp_options.csv"), "WARM_START_FILE", warm)
    tl = os.path.join(wdir, "output", "train_log.csv")
    if os.path.exists(tl):
        os.remove(tl)
    run_exe(wdir)
    return _md5_window(tl, horizon)


def run_slice(N, rate, sidx):
    seeds = slice_seeds(N, rate, sidx)
    label = f"{scenario_label(N, rate)}/coreach/slice{sidx:02d}"
    if not seeds:
        print(f"[{label}] no safe seeds in slice", flush=True)
        os.makedirs(cell_results_dir(N, rate), exist_ok=True)
        open(part_path(N, rate, sidx), "a").close()
        return (N, rate, sidx)
    wdir = setup(N, rate, sidx)
    horizon = D.BASE_SIM_LEN
    os.makedirs(cell_results_dir(N, rate), exist_ok=True)
    raw = part_path(N, rate, sidx)
    done = set()
    if os.path.exists(raw):
        for row in csv.reader(open(raw)):
            if row:
                done.add(int(row[0]))
    todo = [s for s in seeds if s not in done]
    print(f"[{label}] {len(todo)} to run ({len(done)} done)", flush=True)
    with open(raw, "a", newline="") as f:
        w = csv.writer(f)
        for i, s in enumerate(todo):
            u = _run(wdir, s, "", horizon)          # unprotected
            v = _run(wdir, s, "union.csv", horizon)  # union-protected
            noop = 1 if (u is not None and u == v) else 0
            w.writerow([s, noop]); f.flush()
            if (i + 1) % 250 == 0 or i == len(todo) - 1:
                print(f"[{label}] {i + 1}/{len(todo)}", flush=True)
    return (N, rate, sidx)


def merge_cell(N, rate):
    rows = {}
    for sidx in range(SLICES):
        p = part_path(N, rate, sidx)
        if not os.path.exists(p):
            continue
        for row in csv.reader(open(p)):
            if row:
                rows[int(row[0])] = int(row[1])
    noop = sum(rows.values())
    diverge = sorted(s for s, k in rows.items() if k == 0)
    os.makedirs(cell_results_dir(N, rate), exist_ok=True)
    summ = os.path.join(CO_RESULTS, "coreach_summary.csv")
    hdr = not os.path.exists(summ)
    with open(summ, "a", newline="") as f:
        w = csv.writer(f)
        if hdr:
            w.writerow(["N", "rate", "safe_tested", "noop", "noop_frac", "diverge_seeds"])
        n = len(rows)
        w.writerow([N, f"{rate:.2f}", n, noop,
                    f"{noop/n:.4f}" if n else "0", " ".join(map(str, diverge[:30]))])
    print(f"[merge] {scenario_label(N, rate)}: {noop}/{len(rows)} no-op, "
          f"{len(diverge)} diverge {diverge[:10]}")
    return rows


def prove(jobs):
    tasks = [(N, r, s) for N in (2, 5) for r in (1.00, 2.00) for s in range(SLICES)]
    print(f"PROVE coreach: {len(tasks)} slice-jobs")
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        for fut in as_completed({ex.submit(run_slice, *t): t for t in tasks}):
            fut.result()
    for N in (2, 5):
        for r in (1.00, 2.00):
            merge_cell(N, r)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cell"); c.add_argument("--N", type=int, required=True)
    c.add_argument("--rate", type=float, required=True); c.add_argument("--slice", type=int, required=True)
    m = sub.add_parser("merge"); m.add_argument("--N", type=int, required=True)
    m.add_argument("--rate", type=float, required=True)
    pr = sub.add_parser("prove"); pr.add_argument("--jobs", type=int, default=8)
    args = ap.parse_args()
    if args.cmd == "cell":
        run_slice(args.N, args.rate, args.slice)
    elif args.cmd == "merge":
        merge_cell(args.N, args.rate)
    elif args.cmd == "prove":
        prove(args.jobs)


if __name__ == "__main__":
    main()
