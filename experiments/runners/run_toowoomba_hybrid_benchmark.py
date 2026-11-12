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

Output: work/toowoomba_hybrid_benchmark/<policy>/b{b}_c{c}/benchmark.csv ;
harvested to results/toowoomba_hybrid_benchmark/<policy>/... ; grid summary
printed + summary.csv. The constraint set this benchmarks is the one the sweep
learned under the same stall policy, so the policy is part of both paths.

Usage:
    python run_toowoomba_hybrid_benchmark.py                 # full 4x3 grid, jobs=12
    python run_toowoomba_hybrid_benchmark.py --jobs 6
    python run_toowoomba_hybrid_benchmark.py --constraints <path/to/constraints.csv>
"""
import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, CAMPAIGN_DIR)
from _run_toowoomba_helpers import (  # noqa: E402
    set_csv_option, set_toowoomba_rates, parse_dlexp_log,
    load_csv_log, run_exe, append_csv_row, apply_drain, bench_outcome,
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
# PURE=1 benchmarks the pure sweep's learned sets instead, each at the rate it
# was trained at, with the engineered PL constraints off as in its training. It
# writes under its own work and results roots, so no hybrid file is touched.
PURE = os.environ.get("PURE", "0") == "1"
_ROOT = "toowoomba_pure_benchmark" if PURE else "toowoomba_hybrid_benchmark"
WORK_ROOT = os.path.join(CAMPAIGN_DIR, "work", _ROOT)
RESULTS_ROOT = os.path.join(CAMPAIGN_DIR, "results", _ROOT)


def stall_policy():
    """What happens to a replication that never resolved within the iteration
    cap: `bank` keeps its constraints, `discard` throws them away. The rationale
    and the measurement behind it are in run_corridor.py. No learning happens
    here, but the set being benchmarked was learned under one of the two
    policies, so the policy selects which set to load and where to write.

    Read from the environment so pool workers inherit it whatever start method
    is used.
    """
    p = os.environ.get("STALL_POLICY", "bank")
    if p not in ("bank", "discard"):
        raise ValueError(f"STALL_POLICY must be 'bank' or 'discard', got {p!r}")
    return p


def default_constraints():
    """The learned hybrid set (5 constraints, rate-independent) from this
    policy's sweep. A function and not a constant because the policy is read at
    call time, after main() has put the --policy value in the environment."""
    return os.path.join(CAMPAIGN_DIR, "work", "toowoomba_bsweep",
                        stall_policy(), "hybrid", "b0.50_c0.50",
                        "constraints.csv")

def pure_constraints(b, c):
    """The pure sweep's learned set for this rate, from this policy's sweep."""
    return os.path.join(CAMPAIGN_DIR, "work", "toowoomba_bsweep", stall_policy(),
                        "pure", f"b{b:.2f}_c{c:.2f}", "constraints.csv")

# Paper tab:tmba_benchmark grid; under PURE, the pure sweep's rates.
if PURE:
    B_RATES = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
    C_RATES = [0.50]
else:
    B_RATES = [0.25, 0.75, 1.25, 1.75]
    C_RATES = [0.25, 0.50, 0.75]
WARMUP = 24
HORIZON = 168 + WARMUP
# Seeds 1 to 100 are training seeds too (review comment 1.8). BENCH_SEED_START=100000
# benchmarks the corridor's held-out range instead, into benchmark_heldout.csv
# and summary_heldout.csv beside the originals. BENCH_SEED_COUNT=10000 extends
# the held-out file to the corridor's 10,000 seeds, skipping seeds already in it.
BENCHMARK_SEEDS = int(os.environ.get("BENCH_SEED_COUNT", "100"))
BENCH_SEED_START = int(os.environ.get("BENCH_SEED_START", "1"))
BENCH_SUFFIX = "" if BENCH_SEED_START == 1 else "_heldout"
BENCH_SEEDS = range(BENCH_SEED_START, BENCH_SEED_START + BENCHMARK_SEEDS)
# OD_LOG=1 also keeps every train's wait, by origin-destination pair, for the
# mean and 95th percentile that review comment 1.m2 asks for. The engine writes
# train_metric_log.csv on every deadlock_avoidance_exp run and overwrites it on
# the next, so each seed's rows go to od_waits*.csv before the next seed starts.
# The run gets its own suffix, so it reruns seeds the held-out file already has.
OD_LOG = os.environ.get("OD_LOG", "0") == "1"
if OD_LOG:
    BENCH_SUFFIX += "_od"
OD_HEADER = ["seed", "origin", "destination", "wait", "spawn_delay"]


def sdir_for(b, c):
    return os.path.join(WORK_ROOT, stall_policy(), f"b{b:.2f}_c{c:.2f}")


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
    set_csv_option(runctrl, "pl_constraints", 0 if PURE else 1)
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


def _stats_window(runctrl):
    """[warmup, start_warmdown] from the run's own runctrl. The engine's own
    statistics count a train only if it completes inside this window, so the
    per-train figures are filtered the same way."""
    warmup, warmdown = 0.0, 0.0
    for row in csv.reader(open(runctrl, newline="")):
        if row and row[0].strip() == "warmup":
            warmup = float(row[1])
        elif row and row[0].strip() == "start_warmdown":
            warmdown = float(row[1])
    return warmup, warmdown


def od_rows(seed, output_dir, runctrl):
    """One row per train completing in the statistics window: origin, final
    destination (the last entry of job_list), total wait, and spawn delay
    (blank where the log leaves it blank)."""
    path = os.path.join(output_dir, "train_metric_log.csv")
    if not os.path.exists(path):
        return []
    lo, hi = _stats_window(runctrl)
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                done = float(r.get("completion_time") or 0)
                wait = float(r.get("total_wait_time") or 0)
            except ValueError:
                continue
            if done < lo or (hi > 0 and done > hi):
                continue
            dests = re.findall(r"\(([^|)]+)\|", r.get("job_list") or "")
            rows.append([seed, r.get("start_loc") or "?", dests[-1] if dests else "?",
                         f"{wait:.6f}", (r.get("spawn_delay") or "").strip()])
    return rows


def benchmark(b, c, constraints_src):
    label = f"b{b:.2f}_c{c:.2f}"
    sdir = setup(b, c, constraints_src)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    bench_path = os.path.join(sdir, f"benchmark{BENCH_SUFFIX}.csv")

    existing, done = load_csv_log(bench_path)
    deadlocks = sum(1 for r in existing if int(r.get("deadlocked", 0)))
    od_path = os.path.join(sdir, f"od_waits{BENCH_SUFFIX}.csv")
    if OD_LOG and os.path.exists(od_path):
        # a seed killed between its od rows and its benchmark row is rerun, so
        # drop any od rows of a seed the benchmark file does not yet have
        keep = [r for r in csv.reader(open(od_path, newline=""))][1:]
        keep = [r for r in keep if r and int(r[0]) in done]
        with open(od_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(OD_HEADER)
            w.writerows(keep)
    for s in BENCH_SEEDS:
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
        failed, clear = bench_outcome(r)
        if failed:
            deadlocks += 1
        if OD_LOG:
            new = not os.path.exists(od_path)
            with open(od_path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(OD_HEADER)
                w.writerows(od_rows(s, output_dir, runctrl))
        append_csv_row(bench_path,
            ["seed", "throughput", "cpu_time", "deadlocked", "drained_clear"],
            [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
             failed, clear])
    # mean throughput over deadlock-free seeds
    rows, _ = load_csv_log(bench_path)
    thru = [float(x["throughput"]) for x in rows if int(x.get("deadlocked", 0)) == 0]
    mean_thru = sum(thru) / len(thru) if thru else 0.0
    print(f"[{label}] done: mean_thru={mean_thru:.2f}, {deadlocks} deadlocks/{BENCHMARK_SEEDS}")
    rdir = os.path.join(RESULTS_ROOT, stall_policy(), label)
    os.makedirs(rdir, exist_ok=True)
    if os.path.exists(bench_path):
        shutil.copy2(bench_path, os.path.join(rdir, f"benchmark{BENCH_SUFFIX}.csv"))
    if OD_LOG and os.path.exists(od_path):
        shutil.copy2(od_path, os.path.join(rdir, os.path.basename(od_path)))
    return {"b": b, "c": c, "mean_thru": round(mean_thru, 2), "deadlocks": deadlocks}


def worker(spec):
    b, c, constraints_src = spec
    try:
        return benchmark(b, c, constraints_src)
    except Exception as e:
        print(f"[b{b:.2f}_c{c:.2f}] CRASH: {e}")
        return {"b": b, "c": c, "mean_thru": -1, "deadlocks": -1, "crashed": True}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=12)
    p.add_argument("--constraints", default=None,
                   help="learned hybrid constraint set (default: this policy's "
                        "b-sweep b0.50)")
    p.add_argument("--policy", choices=["bank", "discard"],
                   default=os.environ.get("STALL_POLICY", "bank"),
                   help="which sweep's learned set to benchmark; both arms are "
                        "run and compared, and the policy is part of the paths")
    p.add_argument("--cells", nargs="+",
                   help="b{X.XX}_c{X.XX} cells to run, one PBS job each; the "
                        "summary is written only on a run of the full grid, which "
                        "skips every seed already done")
    args = p.parse_args()
    os.environ["STALL_POLICY"] = args.policy
    if PURE:
        args.constraints = pure_constraints(B_RATES[0], C_RATES[0])
    elif args.constraints is None:
        args.constraints = default_constraints()

    if not os.path.exists(args.constraints):
        print(f"ERROR: constraints file not found: {args.constraints}")
        sys.exit(1)
    n_constr = len(set(r[0] for r in csv.reader(open(args.constraints))
                       if r and r[0] != "constraint_id"))
    print(f"Toowoomba {'pure' if PURE else 'hybrid'} benchmark: {len(B_RATES)}x{len(C_RATES)} grid, "
          f"{BENCHMARK_SEEDS} seeds | constraints={args.constraints} ({n_constr} ids) "
          f"| jobs={args.jobs}")

    specs = [(b, c, pure_constraints(b, c) if PURE else args.constraints)
             for b in B_RATES for c in C_RATES
             if not args.cells or f"b{b:.2f}_c{c:.2f}" in args.cells]
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            results = list(ex.map(worker, specs))
    else:
        results = [worker(s) for s in specs]

    crashed = [r for r in results if r.get("crashed")]
    if args.cells:
        return 1 if crashed else 0

    summary_dir = os.path.join(RESULTS_ROOT, stall_policy())
    os.makedirs(summary_dir, exist_ok=True)
    with open(os.path.join(summary_dir, f"summary{BENCH_SUFFIX}.csv"), "w", newline="") as f:
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
    print(f"\nSummary: {summary_dir}/summary.csv")

    # A crashed cell writes -1 into the grid; exiting 0 would have the harness
    # record the job as complete with that -1 standing in for a result.
    crashed = [r for r in results if r.get("crashed")]
    if crashed:
        print(f"FAILED: {len(crashed)} of {len(specs)} cells crashed "
              + ", ".join(f"b{r['b']:.2f}_c{r['c']:.2f}" for r in crashed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
