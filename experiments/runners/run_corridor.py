#!/usr/bin/env python3
"""
Full-campaign driver for the 2026-05-20 pruning rerun.

Runs all 45 scenarios (5 corridor sizes x 9 spawn rates) under the new
global-single pruning algorithm with the rectified bail-out criteria:
  * convergence: 100 consecutive clean seeds
  * bail-out: seed count > 3000 OR total constraints > 25000

Folder structure (relative to this script):
    N{XX}/r{X.XX}/
        input/                  scenario config (auto-populated on first run)
        output/                 exe output (overwritten each seed)
        training_log.csv        per-seed training summary
        constraints.csv         final union of learned constraints
        warm_constraints.csv    working warm-start file
        benchmark.csv           per-seed benchmark (learned vs PL)

Usage:
    python run_campaign.py                       # all 45 scenarios, --jobs 12
    python run_campaign.py --jobs 6              # fewer workers
    python run_campaign.py --scenarios N10:r3.00 # subset
    python run_campaign.py --setup-only          # create dirs+configs, don't run
    python run_campaign.py --skip-training       # benchmark only
    python run_campaign.py --skip-benchmark      # training only

Resumable: each scenario reads its own training_log.csv / benchmark.csv on
start. Re-running picks up from the last completed seed.
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

# === Paths ===

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))          # .../runners
CAMPAIGN_DIR = os.path.dirname(SCRIPT_DIR)                       # the set folder
SET_LABEL = "corridor"
# Heavy artifacts (input/, output/, constraints.csv, warm_constraints.csv) live
# under work/ and stay on the VM; run_all.py harvests only the lean analysis
# CSVs into results/. WORK_DIR is the base for every scenario dir.
WORK_DIR = os.path.join(CAMPAIGN_DIR, "work", SET_LABEL)


def _find_repo_root(start):
    """Walk up from `start` looking for the repo root (the dir that contains DesRail/).

    Allows the campaign folder to be staged anywhere (e.g. C:\\working\\) so
    long as DesRail/ lives alongside it (or two levels up, as in the
    in-repo layout: .../DESLEARN/experiments/<campaign>/).
    """
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
    # Fall back to in-repo assumption: two levels up from campaign dir.
    return os.path.dirname(os.path.dirname(CAMPAIGN_DIR))


REPO_ROOT = _find_repo_root(CAMPAIGN_DIR)

# Reuse canonical helpers from run_experiments.py (now lives in CAMPAIGN_DIR)
sys.path.insert(0, CAMPAIGN_DIR)
from run_experiments import (  # noqa: E402
    set_csv_option,
    set_spawn_rate,
    apply_drain,
    parse_dlexp_log,
    load_csv_log,
    run_exe,
    append_csv_row,
    rate_label,
    scenario_label,
)

# === Campaign parameters ===

CORRIDOR_SIZES = [2, 5, 10, 15, 20]
SPAWN_RATES = [1.0, 1.25, 1.50, 1.75, 2.0, 2.25, 2.50, 2.75, 3.0]
CLEAN_THRESHOLD = 100
MAX_SEEDS = 10000           # rectified bail-out (was 5000 in run_experiments.py)
MAX_CONSTRAINTS = 20000    # new bail-out criterion for the rerun
MAX_ITERATIONS_TRAINING = 2000


def scenario_dir(N, rate):
    return os.path.join(WORK_DIR, f"N{N:02d}", rate_label(rate))


def setup_scenario(N, rate):
    """Create scenario subdir and populate input/ with base config + rate."""
    sdir = scenario_dir(N, rate)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # Copy base configs from experiments/configs/N{XX}/input/
    base_input = os.path.join(CAMPAIGN_DIR, "configs", f"N{N:02d}", "input")
    for fname in os.listdir(base_input):
        src = os.path.join(base_input, fname)
        dst = os.path.join(input_dir, fname)
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)

    # Set scenario-specific spawn rate
    set_spawn_rate(os.path.join(input_dir, "open_loop_spawners.csv"), rate)

    # Default training-mode options (will be reset per-call anyway)
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "tree")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "pl_constraints", 0)
    # Drain: the learner detects late deadlocks (within DEADLOCK_TIMEOUT of the
    # stated horizon) so it learns cuts for them and convergence is judged on a
    # genuinely deadlock-free tail. Horizon = the config's sim_len (192).
    apply_drain(runctrl)

    return sdir


# === Training phase ===

def run_training_scenario(N, rate):
    """Run training loop for one scenario. Returns (final_count, total_seeds, bail_reason)."""
    label = scenario_label(N, rate)
    sdir = setup_scenario(N, rate)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(sdir, "training_log.csv")
    constraints_result = os.path.join(sdir, "constraints.csv")
    warm_file = os.path.join(sdir, "warm_constraints.csv")

    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "tree")
    set_csv_option(runctrl, "pl_constraints", 0)

    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)

    existing_rows, completed_seeds = load_csv_log(training_log)
    consecutive_clean = 0
    for row in reversed(existing_rows):
        if int(row["new_constraints"]) == 0:
            consecutive_clean += 1
        else:
            break
    seed = (max(completed_seeds) + 1) if completed_seeds else 1
    total_constraints = (int(existing_rows[-1]["total_constraints"])
                         if existing_rows else 0)
    training_seeds_count = sum(
        1 for r in existing_rows if int(r["new_constraints"]) > 0)

    if consecutive_clean >= CLEAN_THRESHOLD:
        print(f"[{label}] Already converged: {total_constraints}c, "
              f"{consecutive_clean} clean")
        return total_constraints, seed - 1, "already_converged"

    bail_reason = None

    while consecutive_clean < CLEAN_THRESHOLD and seed <= MAX_SEEDS:
        if total_constraints > MAX_CONSTRAINTS:
            bail_reason = "max_constraints"
            break

        set_csv_option(runctrl, "seed", seed)
        if os.path.exists(warm_file):
            set_csv_option(dlexp_opts, "WARM_START_FILE", "warm_constraints.csv")
        else:
            set_csv_option(dlexp_opts, "WARM_START_FILE", "")

        try:
            run_exe(sdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] ERROR at seed {seed}: {e}")
            bail_reason = f"exe_error: {e}"
            break

        log_path = os.path.join(output_dir, "dlexp_log.csv")
        if not os.path.exists(log_path):
            print(f"[{label}] ERROR: no dlexp_log.csv at seed {seed}")
            bail_reason = "no_log"
            break

        results = parse_dlexp_log(log_path)
        if not results:
            print(f"[{label}] ERROR: empty dlexp_log.csv at seed {seed}")
            bail_reason = "empty_log"
            break

        new_constraints = sum(r["new_constraints"] for r in results)
        total_constraints = results[-1]["total_constraints"]
        iterations = len(results)
        trains_completed = results[-1]["trains_completed"]
        trains_per_hr = results[-1]["trains_per_hr"]
        avg_wait = results[-1].get("avg_wait_time", 0.0)
        cpu_time = sum(r["sim_run_time"] for r in results)
        deadlock_found = 1 if new_constraints > 0 else 0

        if new_constraints > 0:
            gen_file = os.path.join(output_dir, "generated_constraints.csv")
            if os.path.exists(gen_file):
                shutil.copy2(gen_file, warm_file)
                shutil.copy2(gen_file, constraints_result)
            consecutive_clean = 0
            training_seeds_count += 1
        else:
            consecutive_clean += 1

        append_csv_row(training_log,
            ["seed", "deadlock_found", "new_constraints", "total_constraints",
             "iterations", "trains_completed", "trains_per_hr",
             "avg_wait_time", "cpu_time"],
            [seed, deadlock_found, new_constraints, total_constraints,
             iterations, trains_completed, f"{trains_per_hr:.4f}",
             f"{avg_wait:.4f}", f"{cpu_time:.3f}"])

        status = "CLEAN" if new_constraints == 0 else f"+{new_constraints}c"
        print(f"[{label}] seed {seed}: {status} "
              f"(total={total_constraints}c, "
              f"{consecutive_clean}/{CLEAN_THRESHOLD} clean)")

        seed += 1

    total_seeds = seed - 1
    if bail_reason is None:
        if consecutive_clean >= CLEAN_THRESHOLD:
            bail_reason = "converged"
        elif seed > MAX_SEEDS:
            bail_reason = "max_seeds"
        else:
            bail_reason = "unknown"

    print(f"[{label}] Training done ({bail_reason}): "
          f"{total_constraints}c from {training_seeds_count} training seeds, "
          f"{total_seeds} total seeds")
    return total_constraints, total_seeds, bail_reason


# === Benchmark phase ===

def run_benchmark_scenario(N, rate):
    """Run benchmark for one scenario, learned vs PL on seeds 1..CLEAN_THRESHOLD."""
    label = scenario_label(N, rate)
    sdir = scenario_dir(N, rate)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(sdir, "training_log.csv")
    benchmark_path = os.path.join(sdir, "benchmark.csv")
    warm_file = os.path.join(sdir, "warm_constraints.csv")
    constraints_result = os.path.join(sdir, "constraints.csv")

    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)

    all_training, _ = load_csv_log(training_log)
    if not all_training:
        print(f"[{label}] No training data, skipping benchmark")
        return
    max_training_seed = max(int(row["seed"]) for row in all_training)
    max_bench = min(CLEAN_THRESHOLD, max_training_seed)
    bench_seeds = list(range(1, max_bench + 1))

    if max_bench < CLEAN_THRESHOLD:
        print(f"[{label}] WARNING: training reached seed {max_training_seed}, "
              f"benchmarking {max_bench} seeds")

    _, completed_bench_seeds = load_csv_log(benchmark_path)

    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "tree")

    has_constraints = os.path.exists(warm_file)

    print(f"[{label}] Benchmarking {len(bench_seeds)} seeds "
          f"({len(completed_bench_seeds)} done)")

    for i, s in enumerate(bench_seeds):
        if s in completed_bench_seeds:
            continue
        set_csv_option(runctrl, "seed", s)

        # Learned constraints
        set_csv_option(runctrl, "pl_constraints", 0)
        set_csv_option(dlexp_opts, "WARM_START_FILE",
                       "warm_constraints.csv" if has_constraints else "")
        try:
            run_exe(sdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] BENCH ERROR (learned) seed {s}: {e}")
            continue
        log_path = os.path.join(output_dir, "dlexp_log.csv")
        learned = parse_dlexp_log(log_path)

        # PL constraints
        set_csv_option(runctrl, "pl_constraints", 1)
        set_csv_option(dlexp_opts, "WARM_START_FILE", "")
        try:
            run_exe(sdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] BENCH ERROR (PL) seed {s}: {e}")
            continue
        pl = parse_dlexp_log(log_path)

        if not learned or not pl:
            print(f"[{label}] BENCH ERROR: empty results seed {s}")
            continue

        append_csv_row(benchmark_path,
            ["seed", "throughput_learned", "cpu_learned", "learned_deadlocked",
             "avg_wait_learned", "avg_spawn_learned",
             "throughput_pl", "cpu_pl", "pl_deadlocked",
             "avg_wait_pl", "avg_spawn_pl"],
            [s,
             f"{learned[0]['trains_per_hr']:.4f}",
             f"{learned[0]['sim_run_time']:.3f}",
             learned[0]["deadlock_found"],
             f"{learned[0]['avg_wait_time']:.6f}",
             f"{learned[0]['avg_spawn_delay']:.6f}",
             f"{pl[0]['trains_per_hr']:.4f}",
             f"{pl[0]['sim_run_time']:.3f}",
             pl[0]["deadlock_found"],
             f"{pl[0]['avg_wait_time']:.6f}",
             f"{pl[0]['avg_spawn_delay']:.6f}"])

        if (i + 1) % 10 == 0 or i == len(bench_seeds) - 1:
            dl_flag = " (PL DEADLOCK!)" if pl[0]["deadlock_found"] else ""
            print(f"[{label}] bench {i + 1}/{len(bench_seeds)}: "
                  f"learned={learned[0]['trains_per_hr']:.2f} "
                  f"PL={pl[0]['trains_per_hr']:.2f}{dl_flag}")

    print(f"[{label}] Benchmark complete")


# === Wrappers for ProcessPoolExecutor ===

def train_wrapper(args):
    N, rate = args
    try:
        return run_training_scenario(N, rate)
    except Exception as e:
        return None, 0, f"crash: {e}"


def bench_wrapper(args):
    N, rate = args
    try:
        run_benchmark_scenario(N, rate)
    except Exception as e:
        print(f"[{scenario_label(N, rate)}] BENCHMARK CRASH: {e}")


def scenario_wrapper(args):
    N, rate = args
    try:
        run_training_scenario(N, rate)
        run_benchmark_scenario(N, rate)
    except Exception as e:
        print(f"[{scenario_label(N, rate)}] SCENARIO CRASH: {e}")


# === CLI ===

def parse_scenarios(specs):
    if not specs:
        return [(N, r) for N in CORRIDOR_SIZES for r in SPAWN_RATES]
    out = []
    for spec in specs:
        n_part, r_part = spec.split(":")
        N = int(n_part.lstrip("N"))
        rate = float(r_part.lstrip("r"))
        out.append((N, rate))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenarios", nargs="+",
                   help="N{XX}:r{X.XX} (default: all 45)")
    p.add_argument("--jobs", type=int, default=12)
    p.add_argument("--setup-only", action="store_true",
                   help="Create folder skeleton and configs, do not run")
    p.add_argument("--skip-training", action="store_true")
    p.add_argument("--skip-benchmark", action="store_true")
    args = p.parse_args()

    scenarios = parse_scenarios(args.scenarios)
    print(f"Scenarios: {len(scenarios)} | jobs={args.jobs} | "
          f"MAX_SEEDS={MAX_SEEDS} | MAX_CONSTRAINTS={MAX_CONSTRAINTS}")

    # Setup phase: create dirs+configs for every scenario in parallel-safe way
    print("Setting up scenario directories...")
    for N, rate in scenarios:
        setup_scenario(N, rate)
    print(f"Setup complete: {len(scenarios)} scenarios under {WORK_DIR}")

    if args.setup_only:
        return 0

    if not args.skip_training and not args.skip_benchmark:
        # Combined training+benchmark per scenario
        worker = scenario_wrapper
    elif args.skip_benchmark:
        worker = train_wrapper
    else:
        worker = bench_wrapper

    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            list(ex.map(worker, scenarios))
    else:
        for sc in scenarios:
            worker(sc)

    print("Campaign complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
