#!/usr/bin/env python3
"""
WP4 held-out residual-risk evaluation for the deployed (frozen) learned
constraint sets from the corridor campaign.

Goal: a genuinely out-of-sample residual-deadlock-risk estimate for the
deployed supervisor. For each (N, spawn-rate) cell we load a *frozen* learned
constraint set, disable constraint learning, and run many held-out seeds
(disjoint from the training seeds) over the fixed training horizon. Failure =
a deadlock detected within the horizon (the benchmark's existing metric); there
is no drain / cool-down phase.

Two evaluation modes, run together in one pool:
  * native -- each cell loads its OWN frozen set (the deployed per-cell
    supervisor). Also runs a paired unprotected "before" arm on identical seeds.
  * union (cross-rate cut) -- each cell loads the per-N UNION of the converged
    rate sets (a single rate-agnostic supervisor), evaluated at every rate.
    Motivation: the doomed-state basin is rate-independent (fixed by topology);
    spawn rate only controls how many replicates are needed to expose it. So a
    low-rate-trained set under-samples the basin and shows residual risk out of
    sample; deploying the more-complete union should collapse that residual.
    After-arm only (the unprotected "before" is rate-only, already in native).

Methodological guarantees (a reviewer will check these):
  * frozen constraints  -> WARM_START_FILE points at a read-only copy of the
    frozen/union set at the work-dir root; generated constraints are never
    merged back, so the set stays frozen across all seeds.
  * learning off         -> MAX_ITERATIONS = 1 (single simulation pass).
  * disjoint test seeds  -> held-out seeds start at SEED_START (100000); the
    training campaign never exceeded seed 2585.
  * fixed stated horizon -> sim_len = 192 h (24 h warmup + 168 h collection),
    identical to training. The bound is conditional on this horizon.
  * identical seeds before vs after -> both arms of a native cell share a seed
    list; "before" = unprotected (no learned constraints, no PL).
  * same binary            -> evaluated by the exact exe that trained the sets
    (bundle x64/Release/DesRail.exe), so no train/eval interpretation gap.

Layout (all under the paper-results bundle; native paths unchanged so an
in-progress native run resumes cleanly):
  work/heldout_eval/N{XX}/r{X.XX}/                       native cell work dir
  work/heldout_eval/_union/N{XX}/union_constraints.csv   per-N union set
  work/heldout_eval/crossrate_union/N{XX}/r{X.XX}/        union cell work dir
  results/heldout_eval/N{XX}/r{X.XX}/heldout_raw.csv               native raw
  results/heldout_eval/crossrate_union/N{XX}/r{X.XX}/heldout_raw.csv  union raw
  results/heldout_eval/heldout_summary.csv                         table + CI
  results/heldout_eval/heldout_pdeadlock.png                       figure

Usage:
  python run_heldout_eval.py --smoke                 # one cheap native cell
  python run_heldout_eval.py --jobs 6                # full campaign (native+union)
  python run_heldout_eval.py --mode native --jobs 6  # native only
  python run_heldout_eval.py --analyze-only          # rebuild summary
  <venv-python> run_heldout_eval.py --figure         # figure (needs matplotlib)
"""

import argparse
import csv
import math
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # the bundle dir
sys.path.insert(0, SCRIPT_DIR)
from run_experiments import (  # noqa: E402
    set_csv_option,
    set_spawn_rate,
    parse_dlexp_log,
    load_csv_log,
    run_exe,
    append_csv_row,
    rate_label,
    scenario_label,
)

# === Parameters ===

GRID_N = [2, 5, 10, 15, 20]
GRID_RATES = [1.00, 2.00, 2.50, 2.75, 3.00]
SEED_START = 100000            # held-out seeds; training never exceeded 2585
DEFAULT_SEEDS = 10000
HORIZON_HOURS = 192            # 24 h warmup + 168 h collection (matches training)
# Cells that did NOT converge in training (bailed on seed/constraint budget).
# Empirical k/N + two-sided Clopper-Pearson CI rather than a Rule-of-Three bound.
# Also excluded from the per-N union (union = the certified/converged sets only).
NON_CONVERGED = {(15, 3.00), (20, 2.75), (20, 3.00)}

# Native per-cell held-out seed counts. Big learned sets are slow to load+eval
# per run (measured "after"-arm cost: N20/r3.00 ~19 s, N20/r2.75 ~18 s,
# N15/r3.00 ~16 s, N20/r2.50 ~5 s, N15/r2.75 ~2.6 s, N15/r2.50 ~1.2 s; all
# others <=0.86 s). These get fewer seeds so the campaign fits overnight on 6
# cores; every other cell uses DEFAULT_SEEDS. Exact per-cell n + range recorded
# in the summary. Reproducible (fixed counts, not a wall-clock cap).
SEEDS_SCHEDULE = {
    (15, 2.50): 7500,
    (15, 2.75): 3500,
    (15, 3.00): 1000,
    (20, 2.50): 2000,
    (20, 2.75): 1000,
    (20, 3.00): 1000,
}

# Cross-rate (union) per-N seed counts. Union size ~ the largest converged rate
# set for that N, so cost grows with N (N15 union ~3 s/run, N20 union ~6 s/run).
CROSS_SEEDS = {2: 10000, 5: 8000, 10: 3000, 15: 1200, 20: 1000}

MODES = ("native", "union")
MODE_SUBDIR = {"native": "", "union": "crossrate_union"}

CONFIGS_DIR = os.path.join(SCRIPT_DIR, "configs")
CORRIDOR_WORK = os.path.join(SCRIPT_DIR, "work", "corridor")     # frozen sets live here
HELDOUT_WORK = os.path.join(SCRIPT_DIR, "work", "heldout_eval")
HELDOUT_RESULTS = os.path.join(SCRIPT_DIR, "results", "heldout_eval")
UNION_DIR = os.path.join(HELDOUT_WORK, "_union")

RAW_HEADER = ["seed",
              "after_deadlock", "after_throughput",
              "before_deadlock", "before_throughput"]


def seeds_for(N, rate, default):
    return SEEDS_SCHEDULE.get((N, rate), default)


# === Paths ===

def cell_work_dir(N, rate, mode):
    return os.path.join(HELDOUT_WORK, MODE_SUBDIR[mode], f"N{N:02d}", rate_label(rate))


def cell_results_dir(N, rate, mode):
    return os.path.join(HELDOUT_RESULTS, MODE_SUBDIR[mode], f"N{N:02d}", rate_label(rate))


def frozen_constraints_path(N, rate):
    return os.path.join(CORRIDOR_WORK, f"N{N:02d}", rate_label(rate), "constraints.csv")


def union_path(N):
    return os.path.join(UNION_DIR, f"N{N:02d}", "union_constraints.csv")


# === Per-N union of the converged rate sets ===

def _read_constraint_groups(path):
    """Return (header, [(cid, [rows])...]) preserving row order within each
    constraint id."""
    rows = list(csv.reader(open(path, newline="")))
    header = rows[0]
    order = {}
    groups = []
    for r in rows[1:]:
        if not r:
            continue
        cid = r[0]
        if cid not in order:
            order[cid] = len(groups)
            groups.append((cid, []))
        groups[order[cid]][1].append(r)
    return header, groups


def _canon_key(group_rows):
    """Constraint identity independent of its id: the sorted multiset of each
    row's columns after the id column."""
    return tuple(sorted(tuple(r[1:]) for r in group_rows))


def build_union_constraints(N):
    """Merge the frozen sets of all CONVERGED rates for N into one deduplicated,
    re-indexed constraint file. Returns (path, n_unique)."""
    out_dir = os.path.join(UNION_DIR, f"N{N:02d}")
    os.makedirs(out_dir, exist_ok=True)
    out = union_path(N)

    header = None
    seen = set()
    unique = []
    used_rates = []
    for rate in GRID_RATES:
        if (N, rate) in NON_CONVERGED:
            continue
        src = frozen_constraints_path(N, rate)
        if not os.path.exists(src):
            continue
        h, groups = _read_constraint_groups(src)
        header = h
        used_rates.append(rate)
        for _cid, grows in groups:
            key = _canon_key(grows)
            if key in seen:
                continue
            seen.add(key)
            unique.append(grows)

    if header is None:
        raise FileNotFoundError(f"No converged frozen sets found for N={N}")

    # Atomic write: the per-N union file is shared, and per-rate union jobs may
    # rebuild it concurrently. The content is deterministic (same corridor sets ->
    # same union), so write to a per-process temp and os.replace() it into place
    # (atomic on POSIX) — a reader always sees a complete file, never a partial
    # one being written by another job.
    tmp = out + f".tmp.{os.getpid()}"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for i, grows in enumerate(unique):
            nid = f"dl_{i}"
            for r in grows:
                w.writerow([nid] + list(r[1:]))
    os.replace(tmp, out)
    return out, len(unique), used_rates


# === Cell setup ===

def setup_cell(N, rate, mode):
    """Isolated work dir: base config + spawn rate + a read-only copy of the
    frozen set for this mode at the work-dir ROOT (WARM_START_FILE resolves
    relative to the run cwd, not input/). Never touches work/corridor/."""
    wdir = cell_work_dir(N, rate, mode)
    input_dir = os.path.join(wdir, "input")
    output_dir = os.path.join(wdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    base_input = os.path.join(CONFIGS_DIR, f"N{N:02d}", "input")
    for fname in os.listdir(base_input):
        src = os.path.join(base_input, fname)
        dst = os.path.join(input_dir, fname)
        if os.path.isfile(src):
            shutil.copy2(src, dst)

    set_spawn_rate(os.path.join(input_dir, "open_loop_spawners.csv"), rate)

    if mode == "native":
        frozen_src = frozen_constraints_path(N, rate)
    elif mode == "union":
        frozen_src = union_path(N)
    else:
        raise ValueError(f"unknown mode {mode}")
    if not os.path.exists(frozen_src):
        raise FileNotFoundError(
            f"[{scenario_label(N, rate)}/{mode}] frozen set missing: {frozen_src}")
    frozen_name = "frozen_constraints.csv"
    shutil.copy2(frozen_src, os.path.join(wdir, frozen_name))

    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp, "CONSTRAINT_EVAL", "tree")
    return wdir, frozen_name


def _run_seed(wdir, frozen_name, seed, frozen):
    """Single sim. frozen=True -> load the frozen set. frozen=False -> unprotected
    (no learned, no PL). Returns (deadlock_found, throughput) or None."""
    input_dir = os.path.join(wdir, "input")
    output_dir = os.path.join(wdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp = os.path.join(input_dir, "dlexp_options.csv")

    set_csv_option(runctrl, "seed", seed)
    set_csv_option(runctrl, "pl_constraints", 0)
    set_csv_option(dlexp, "WARM_START_FILE", frozen_name if frozen else "")

    try:
        run_exe(wdir)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"[{os.path.relpath(wdir, HELDOUT_WORK)}] EXE ERROR "
              f"({'after' if frozen else 'before'}) seed {seed}: {e}")
        return None

    rows = parse_dlexp_log(os.path.join(output_dir, "dlexp_log.csv"))
    if not rows:
        return None
    return rows[0]["deadlock_found"], rows[0]["trains_per_hr"]


def run_cell(N, rate, n_seeds, do_before, mode):
    """Collect held-out results for one (mode, N, rate) cell. Resumable."""
    label = f"{scenario_label(N, rate)}/{mode}"
    if mode == "union":
        do_before = False       # unprotected arm is rate-only; lives in native
    wdir, frozen_name = setup_cell(N, rate, mode)
    rdir = cell_results_dir(N, rate, mode)
    os.makedirs(rdir, exist_ok=True)
    raw_path = os.path.join(rdir, "heldout_raw.csv")

    _, done_seeds = load_csv_log(raw_path)
    seeds = list(range(SEED_START, SEED_START + n_seeds))
    todo = [s for s in seeds if s not in done_seeds]
    print(f"[{label}] {len(seeds)} seeds ({len(done_seeds)} done, "
          f"{len(todo)} to run), before={do_before}")

    for i, s in enumerate(todo):
        after = _run_seed(wdir, frozen_name, s, frozen=True)
        if after is None:
            continue
        a_dl, a_tp = after

        b_dl, b_tp = "", ""
        if do_before:
            before = _run_seed(wdir, frozen_name, s, frozen=False)
            if before is not None:
                b_dl, b_tp = before

        append_csv_row(raw_path, RAW_HEADER,
                       [s, a_dl, f"{a_tp:.4f}",
                        b_dl, (f"{b_tp:.4f}" if b_tp != "" else "")])

        if (i + 1) % 500 == 0 or i == len(todo) - 1:
            print(f"[{label}] {i + 1}/{len(todo)} (seed {s}, after_dl={a_dl})")

    print(f"[{label}] cell complete")
    return N, rate, mode


def run_cell_wrapper(args):
    N, rate, n_seeds, do_before, mode = args
    try:
        return run_cell(N, rate, n_seeds, do_before, mode)
    except Exception as e:
        print(f"[{scenario_label(N, rate)}/{mode}] CELL CRASH: {e}")
        return N, rate, mode


# === Statistics (pure stdlib; validated against scipy to 1e-15) ===

def _betacf(a, b, x):
    MAXIT, EPS, FPMIN = 200, 3.0e-12, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delt = d * c
        h *= delt
        if abs(delt - 1.0) < EPS:
            break
    return h


def betai(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _beta_ppf(p, a, b):
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if betai(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def clopper_pearson(k, n, alpha=0.05):
    if n == 0:
        return 0.0, 1.0
    lower = 0.0 if k == 0 else _beta_ppf(alpha / 2.0, k, n - k + 1)
    upper = 1.0 if k == n else _beta_ppf(1.0 - alpha / 2.0, k + 1, n - k)
    return lower, upper


# === Analysis / summary ===

def _cell_stats(N, rate, mode):
    raw_path = os.path.join(cell_results_dir(N, rate, mode), "heldout_raw.csv")
    if not os.path.exists(raw_path):
        return None
    rows, _ = load_csv_log(raw_path)
    n = a_dl = b_n = b_dl = 0
    a_tp = b_tp = 0.0
    for r in rows:
        try:
            k = int(r["after_deadlock"])
            tp = float(r["after_throughput"])
        except (KeyError, ValueError):
            continue
        n += 1
        a_dl += k
        a_tp += tp
        if r.get("before_deadlock", "") not in ("", None):
            try:
                b_dl += int(r["before_deadlock"])
                b_tp += float(r["before_throughput"])
                b_n += 1
            except ValueError:
                pass
    if n == 0:
        return None
    cp_lo, cp_hi = clopper_pearson(a_dl, n)
    converged = (N, rate) not in NON_CONVERGED
    return {
        "N": N, "rate": rate, "mode": mode, "converged": converged,
        "n": n, "seed_lo": SEED_START, "seed_hi": SEED_START + n - 1,
        "a_dl": a_dl, "a_p": a_dl / n, "cp_lo": cp_lo, "cp_hi": cp_hi,
        "rot_upper": (3.0 / n if a_dl == 0 else float("nan")),
        "a_tp": a_tp / n,
        "b_n": b_n, "b_dl": b_dl,
        "b_p": (b_dl / b_n) if b_n else float("nan"),
        "b_tp": (b_tp / b_n) if b_n else float("nan"),
    }


def build_summary():
    os.makedirs(HELDOUT_RESULTS, exist_ok=True)
    out = os.path.join(HELDOUT_RESULTS, "heldout_summary.csv")
    header = ["mode", "N", "spawn_rate", "converged", "horizon_hours",
              "seed_lo", "seed_hi", "n_seeds",
              "after_deadlocks", "after_p_deadlock",
              "after_rot_upper95", "after_cp_lo95", "after_cp_hi95",
              "after_mean_throughput",
              "before_n", "before_deadlocks", "before_p_deadlock",
              "before_mean_throughput"]
    rows = []
    for mode in MODES:
        for N in GRID_N:
            for rate in GRID_RATES:
                s = _cell_stats(N, rate, mode)
                if s is None:
                    continue
                rows.append([
                    mode, s["N"], f"{s['rate']:.2f}", int(s["converged"]),
                    HORIZON_HOURS, s["seed_lo"], s["seed_hi"], s["n"],
                    s["a_dl"], f"{s['a_p']:.6f}",
                    ("" if math.isnan(s["rot_upper"]) else f"{s['rot_upper']:.6f}"),
                    f"{s['cp_lo']:.6f}", f"{s['cp_hi']:.6f}", f"{s['a_tp']:.4f}",
                    s["b_n"], s["b_dl"],
                    ("" if math.isnan(s["b_p"]) else f"{s['b_p']:.6f}"),
                    ("" if math.isnan(s["b_tp"]) else f"{s['b_tp']:.4f}"),
                ])
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"Summary written: {out} ({len(rows)} rows)")

    print(f"\n{'mode':>7} {'cell':>10} {'after k/N':>13} {'p(dl)':>9} "
          f"{'95% bound':>22} {'thru':>7} {'before p':>9}")
    for r in rows:
        mode, N, rate = r[0], r[1], r[2]
        k, n = r[8], r[7]
        conv = int(r[3])
        bound = (f"RoT<={r[10]}" if (conv and r[10]) else
                 f"CP[{r[11]},{r[12]}]")
        print(f"{mode:>7} {('N%02d/r%s' % (N, rate)):>10} "
              f"{('%d/%d' % (k, n)):>13} {r[9]:>9} {bound:>22} "
              f"{r[13]:>7} {r[16]:>9}")
    return out


# === Figure (needs matplotlib; run with a venv python) ===

def build_figure():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib unavailable; run --figure with a venv python. Skipping.")
        return None

    stats = {(m, N, r): _cell_stats(N, r, m)
             for m in MODES for N in GRID_N for r in GRID_RATES}
    stats = {k: v for k, v in stats.items() if v is not None}
    if not stats:
        print("No data to plot.")
        return None

    FLOOR = 8e-5   # log-axis floor; exact-0 cells drawn here as "<= RoT" markers
    fig, axes = plt.subplots(2, len(GRID_N), figsize=(4 * len(GRID_N), 6.4),
                             sharex=True)
    for j, N in enumerate(GRID_N):
        top, bot = axes[0][j], axes[1][j]
        nr = [r for r in GRID_RATES if ("native", N, r) in stats]
        ur = [r for r in GRID_RATES if ("union", N, r) in stats]

        # --- top: protection overview (full scale) ---
        if nr:
            top.plot(nr, [stats[("native", N, r)]["a_p"] for r in nr], "o-",
                     color="#1f77b4", label="native (per-cell)")
            bef = [(r, stats[("native", N, r)]["b_p"]) for r in nr
                   if not math.isnan(stats[("native", N, r)]["b_p"])]
            if bef:
                br, bp = zip(*bef)
                top.plot(br, bp, "s--", color="#d62728", label="unprotected")
        if ur:
            top.plot(ur, [stats[("union", N, r)]["a_p"] for r in ur], "^-",
                     color="#2ca02c", label="union (rate-agnostic)")
        top.set_title(f"N = {N}")
        top.set_ylim(-0.03, 1.03)
        top.grid(True, alpha=0.3)

        # --- bottom: residual detail (log), native vs union ---
        def _plot_resid(ax, rates, mode, color, marker, label):
            xs_pos, ys_pos, xs_zero, ys_zero = [], [], [], []
            for r in rates:
                s = stats[(mode, N, r)]
                if s["a_dl"] > 0:
                    xs_pos.append(r); ys_pos.append(s["a_p"])
                else:
                    xs_zero.append(r); ys_zero.append(s["cp_hi"])  # RoT upper
            # connect point estimates and the RoT-upper stand-ins for a line
            allx = [r for r in rates]
            ally = [stats[(mode, N, r)]["a_p"] if stats[(mode, N, r)]["a_dl"] > 0
                    else FLOOR for r in rates]
            ax.plot(allx, ally, "-", color=color, alpha=0.4)
            if xs_pos:
                ax.plot(xs_pos, ys_pos, marker, color=color, label=label)
            if xs_zero:
                ax.plot(xs_zero, ys_zero, marker, mfc="white", color=color,
                        label=f"{label}: 0 (≤ 95% upper)")
            # 95% CP upper band
            ax.plot(rates, [max(stats[(mode, N, r)]["cp_hi"], FLOOR) for r in rates],
                    ":", color=color, alpha=0.5)
        if nr:
            _plot_resid(bot, nr, "native", "#1f77b4", "o", "native")
        if ur:
            _plot_resid(bot, ur, "union", "#2ca02c", "^", "union")
        bot.set_yscale("log")
        bot.set_ylim(FLOOR * 0.7, 0.4)
        bot.set_xlabel("spawn rate")
        bot.grid(True, which="both", alpha=0.25)

    axes[0][0].set_ylabel("p(deadlock)  [full scale]")
    axes[1][0].set_ylabel("p(deadlock)  [log; dotted = 95% CP upper]")
    axes[0][0].legend(fontsize=8, loc="center left")
    axes[1][0].legend(fontsize=7, loc="lower right")
    fig.suptitle(f"Held-out residual deadlock risk (horizon {HORIZON_HOURS} h, "
                 f"seeds from {SEED_START}); top = protection, "
                 f"bottom = native-vs-union residual detail")
    fig.tight_layout()
    out = os.path.join(HELDOUT_RESULTS, "heldout_pdeadlock.png")
    fig.savefig(out, dpi=150)
    print(f"Figure written: {out}")
    return out


# === CLI ===

def parse_scenarios(specs):
    if not specs:
        return [(N, r) for N in GRID_N for r in GRID_RATES]
    out = []
    for spec in specs:
        n_part, r_part = spec.split(":")
        out.append((int(n_part.lstrip("N")), float(r_part.lstrip("r"))))
    return out


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenarios", nargs="+", help="e.g. N10:r2.50 (default: full grid)")
    p.add_argument("--seeds", type=int, default=DEFAULT_SEEDS,
                   help=f"native seeds per cell (default {DEFAULT_SEEDS})")
    p.add_argument("--jobs", type=int, default=1, help="parallel cell workers")
    p.add_argument("--mode", choices=["native", "union", "both"], default="both")
    p.add_argument("--no-before", action="store_true",
                   help="skip the paired unprotected 'before' arm (native)")
    p.add_argument("--smoke", action="store_true",
                   help="cheap native sanity run: N05:r1.00, 200 seeds")
    p.add_argument("--analyze-only", action="store_true")
    p.add_argument("--figure", action="store_true")
    args = p.parse_args()

    if args.figure:
        build_figure()
        return 0
    if args.analyze_only:
        build_summary()
        return 0

    if args.smoke:
        tasks = [(5, 1.00, 200, True, "native")]
    else:
        scen = parse_scenarios(args.scenarios)
        do_before = not args.no_before
        tasks = []
        if args.mode in ("native", "both"):
            tasks += [(N, r, seeds_for(N, r, args.seeds), do_before, "native")
                      for N, r in scen]
        if args.mode in ("union", "both"):
            # Build per-N union sets upfront (serial) so pool workers don't race.
            print("Building per-N union sets (converged rates only)...")
            for N in GRID_N:
                _, nu, used = build_union_constraints(N)
                print(f"  N{N:02d}: {nu} unique constraints "
                      f"from rates {[f'{r:.2f}' for r in used]}")
            union_scen = scen if args.scenarios else [(N, r) for N in GRID_N
                                                      for r in GRID_RATES]
            tasks += [(N, r, CROSS_SEEDS.get(N, DEFAULT_SEEDS), False, "union")
                      for N, r in union_scen]

    print(f"\nHeld-out eval: {len(tasks)} cells (start seed {SEED_START}), "
          f"jobs={args.jobs}")
    print(f"Frozen sets:  {CORRIDOR_WORK}")
    print(f"Results:      {HELDOUT_RESULTS}")
    for N, rate, ns, db, mode in tasks:
        tag = "  (reduced: costly)" if (mode == "native"
                                        and (N, rate) in SEEDS_SCHEDULE) else ""
        print(f"  {mode:>7} {scenario_label(N, rate)}: {ns} seeds{tag}")

    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(run_cell_wrapper, t): t for t in tasks}
            for fut in as_completed(futs):
                fut.result()
    else:
        for t in tasks:
            run_cell_wrapper(t)

    build_summary()
    print(f"\nDone. Figure: <venv-python> {os.path.basename(__file__)} --figure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
