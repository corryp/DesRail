#!/usr/bin/env python3
"""
Genuine hybrid throughput benchmark for the Toowoomba junction (2026-06-04).

Motivation: the paper's tab:tmba_benchmark throughput numbers trace to
2026-05-20_pruning_rerun/results/toowoomba/*/benchmark.csv, which are deadlock-free
but whose TRAINING did not converge under pure learning -- i.e. those benchmarks must
have run on engineered constraints, so the "hybrid" throughput table is really the
engineered baseline. The b-sweep has now proven the genuine hybrid (PL + 5 learned
junction constraints) converges, so we can measure a legitimately-hybrid, deadlock-free
throughput table.

This runner loads the converged 5-constraint hybrid set as the warm-start (the set is
rate-independent, verified across the whole b-sweep) and runs 100 benchmark seeds at
each cell of the paper's 4x3 grid (lambda_B in {0.25,0.75,1.25,1.75} x lambda_C in
{0.25,0.50,0.75}), with PL constraints active and MAX_ITERATIONS=1 (no learning).
Records per-seed throughput + deadlock flag; reports mean throughput and deadlock count
per cell, matching the paper table's structure for a like-for-like comparison.

Output: work/toowoomba_hybrid_benchmark/b{b}_c{c}/benchmark.csv ; harvested to
results/toowoomba_hybrid_benchmark/... ; grid summary printed + summary.csv.

Usage:
    python run_toowoomba_hybrid_benchmark.py                 # full 4x3 grid, jobs=12
    python run_toowoomba_hybrid_benchmark.py --jobs 6
    python run_toowoomba_hybrid_benchmark.py --constraints <path/to/constraints.csv>
"""
import argparse
import csv
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, CAMPAIGN_DIR)
from _run_toowoomba_helpers import (  # noqa: E402
    set_csv_option, set_toowoomba_rates, parse_dlexp_log,
    load_csv_log, run_exe, append_csv_row, apply_drain,
)


def _find_repo_root(start):
    override = os.environ.get("DESRAIL_REPO")
    if override:
        return override
    d = start
    for _ in range(6):
        if os.path.isfile(os.path.join(d, "DesRail", "makefile")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.dirname(os.path.dirname(CAMPAIGN_DIR))


REPO_ROOT = _find_repo_root(CAMPAIGN_DIR)
TMBA_INPUT = os.path.join(CAMPAIGN_DIR, "tmba_input")   # frozen Toowoomba config (bundle-local)
WORK_ROOT = os.path.join(CAMPAIGN_DIR, "work", "toowoomba_hybrid_benchmark")
RESULTS_ROOT = os.path.join(CAMPAIGN_DIR, "results", "toowoomba_hybrid_benchmark")
# Default learned hybrid constraint set (5 constraints, rate-independent).
DEFAULT_CONSTRAINTS = os.path.join(
    CAMPAIGN_DIR, "work", "toowoomba_bsweep", "hybrid", "b0.50_c0.50", "constraints.csv")

# Paper tab:tmba_benchmark grid.
B_RATES = [0.25, 0.75, 1.25, 1.75]
C_RATES = [0.25, 0.50, 0.75]
WARMUP = 24
HORIZON = 168 + WARMUP
BENCHMARK_SEEDS = 100


def sdir_for(b, c):
    return os.path.join(WORK_ROOT, f"b{b:.2f}_c{c:.2f}")


def setup(b, c, constraints_src):
    sdir = sdir_for(b, c)
    input_dir = os.path.join(sdir, "input")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(os.path.join(sdir, "output"), exist_ok=True)
    for fname in os.listdir(TMBA_INPUT):
        src = os.path.join(TMBA_INPUT, fname)
        if os.path.isfile(src) and fname.endswith(".csv"):
            dst = os.path.join(input_dir, fname)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
    # blank manual constraints; PL supplied via flag, junction via warm-start.
    with open(os.path.join(input_dir, "signal_constraints.csv"), "w", newline="") as f:
        f.write("str_id,segment / occ_limit,head,target,comment\n")
    # strip engineered pre-cooked constraints (the contamination we fixed earlier).
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)
    # install the learned 5-constraint hybrid set as warm-start.
    warm = os.path.join(sdir, "warm_constraints.csv")
    shutil.copy2(constraints_src, warm)

    set_toowoomba_rates(os.path.join(input_dir, "open_loop_spawners.csv"), b, c)
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "sim_len", HORIZON)
    set_csv_option(runctrl, "warmup", WARMUP)
    set_csv_option(runctrl, "animate", 0)
    set_csv_option(runctrl, "screen_output", 0)
    set_csv_option(runctrl, "log_output", 0)
    set_csv_option(runctrl, "pl_constraints", 1)
    apply_drain(runctrl)   # horizon = HORIZON set above; fair deadlock detection in benchmark
    set_csv_option(os.path.join(input_dir, "main_option.csv"),
                   "enter experiment type", "deadlock_avoidance_exp")
    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp, "CONSTRAINT_EVAL", "tree")
    set_csv_option(dlexp, "START_DEBUG", 99999)
    set_csv_option(dlexp, "MAX_ITERATIONS", 1)           # benchmark: no learning
    set_csv_option(dlexp, "WARM_START_FILE", "warm_constraints.csv")
    set_csv_option(dlexp, "LAST_CONSTRAINT", "")
    return sdir


def benchmark(b, c, constraints_src):
    label = f"b{b:.2f}_c{c:.2f}"
    sdir = setup(b, c, constraints_src)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    bench_path = os.path.join(sdir, "benchmark.csv")

    existing, done = load_csv_log(bench_path)
    deadlocks = sum(1 for r in existing if int(r.get("deadlocked", 0)))
    for s in range(1, BENCHMARK_SEEDS + 1):
        if s in done:
            continue
        set_csv_option(runctrl, "seed", s)
        try:
            run_exe(sdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] ERROR seed {s}: {e}")
            continue
        results = parse_dlexp_log(os.path.join(output_dir, "dlexp_log.csv"))
        if not results:
            continue
        r = results[0]
        if r["deadlock_found"]:
            deadlocks += 1
        append_csv_row(bench_path,
            ["seed", "throughput", "cpu_time", "deadlocked"],
            [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
             r["deadlock_found"]])
    # mean throughput over deadlock-free seeds
    rows, _ = load_csv_log(bench_path)
    thru = [float(x["throughput"]) for x in rows if int(x.get("deadlocked", 0)) == 0]
    mean_thru = sum(thru) / len(thru) if thru else 0.0
    print(f"[{label}] done: mean_thru={mean_thru:.2f}, {deadlocks} deadlocks/{BENCHMARK_SEEDS}")
    rdir = os.path.join(RESULTS_ROOT, label)
    os.makedirs(rdir, exist_ok=True)
    if os.path.exists(bench_path):
        shutil.copy2(bench_path, os.path.join(rdir, "benchmark.csv"))
    return {"b": b, "c": c, "mean_thru": round(mean_thru, 2), "deadlocks": deadlocks}


def worker(spec):
    b, c, constraints_src = spec
    try:
        return benchmark(b, c, constraints_src)
    except Exception as e:
        print(f"[b{b:.2f}_c{c:.2f}] CRASH: {e}")
        return {"b": b, "c": c, "mean_thru": -1, "deadlocks": -1}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=12)
    p.add_argument("--constraints", default=DEFAULT_CONSTRAINTS,
                   help="learned hybrid constraint set (default: b-sweep b0.50)")
    args = p.parse_args()

    if not os.path.exists(args.constraints):
        print(f"ERROR: constraints file not found: {args.constraints}")
        sys.exit(1)
    n_constr = len(set(r[0] for r in csv.reader(open(args.constraints))
                       if r and r[0] != "constraint_id"))
    print(f"Toowoomba hybrid benchmark: {len(B_RATES)}x{len(C_RATES)} grid, "
          f"{BENCHMARK_SEEDS} seeds | constraints={args.constraints} ({n_constr} ids) "
          f"| jobs={args.jobs}")

    specs = [(b, c, args.constraints) for b in B_RATES for c in C_RATES]
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            results = list(ex.map(worker, specs))
    else:
        results = [worker(s) for s in specs]

    os.makedirs(RESULTS_ROOT, exist_ok=True)
    with open(os.path.join(RESULTS_ROOT, "summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lambda_B", "lambda_C", "mean_throughput", "deadlocks"])
        for r in sorted(results, key=lambda x: (x["b"], x["c"])):
            w.writerow([r["b"], r["c"], r["mean_thru"], r["deadlocks"]])

    grid = {(r["b"], r["c"]): r for r in results}
    print("\nMean throughput (trains/hr) [deadlocks]:")
    print("        " + "".join(f"  c={c:.2f}    " for c in C_RATES))
    for b in B_RATES:
        cells = []
        for c in C_RATES:
            r = grid.get((b, c), {})
            cells.append(f"{r.get('mean_thru', 0):5.2f} [{r.get('deadlocks', 0):>3}]")
        print(f"b={b:.2f}  " + "  ".join(cells))
    print(f"\nSummary: {RESULTS_ROOT}/summary.csv")


if __name__ == "__main__":
    main()
