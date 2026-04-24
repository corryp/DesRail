#!/usr/bin/env python3
"""
Toowoomba deadlock avoidance experiment campaign — hybrid PL + learned constraints.

Runs training and benchmark phases across a grid of spawn rates for the
Toowoomba network, varying Brisbane route rates (ols1-4) and corridor
route rates (ols5-6) independently.

Training runs WITH pl_constraints=1, so the learner discovers only the
constraints needed beyond what PL signals already prevent. Benchmark
compares hybrid (PL + learned) vs PL-only (no learned constraints).

Termination criteria for training:
  - Convergence: 100 consecutive deadlock-free seeds
  - Non-convergence: seeds > 3000 OR constraints > 25000 (whichever first)

Usage:
    python run_toowoomba.py                          # Run all 12 scenarios
    python run_toowoomba.py --jobs 4                 # 4 parallel workers
    python run_toowoomba.py --scenarios b0.75:c0.50  # Single scenario
    python run_toowoomba.py --dry-run                # Show what would run
    python run_toowoomba.py --skip-training           # Benchmark only
    python run_toowoomba.py --skip-benchmark         # Training only
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

# === Paths ===

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
EXE_PATH = os.path.join(REPO_ROOT, "x64", "Release", "DesRail.exe")
TMBA_INPUT = os.path.join(REPO_ROOT, "DesRail", "input")

# === Experiment parameters ===

# Brisbane routes: ols1-4
BRISBANE_RATES = [0.25, 0.75, 1.25, 1.75]
# Corridor routes: ols5-6
CORRIDOR_RATES = [0.25, 0.50, 0.75]

WARMUP = 24        # hours
HORIZON = 168 + WARMUP  # 1 week collection + warmup
CLEAN_THRESHOLD = 100       # consecutive clean seeds for convergence
MAX_ITERATIONS_TRAINING = 2000  # per-seed iteration limit
MAX_SEEDS = 3000            # termination: seed count
MAX_CONSTRAINTS = 25000     # termination: constraint count
BENCHMARK_SEEDS = 100       # number of benchmark seeds
EXE_TIMEOUT = 28800         # 8 hours max per exe invocation


# === Helpers ===

def rate_label(brisbane_rate, corridor_rate):
    return f"b{brisbane_rate:.2f}_c{corridor_rate:.2f}"


def set_csv_option(filepath, option_name, value):
    rows = []
    with open(filepath, newline="") as f:
        for row in csv.reader(f):
            rows.append(row)
    found = False
    for row in rows:
        if row and row[0].strip() == option_name:
            row[1] = str(value)
            found = True
            break
    if not found:
        raise ValueError(f"Option '{option_name}' not found in {filepath}")
    with open(filepath, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def set_toowoomba_rates(filepath, brisbane_rate, corridor_rate):
    rows = []
    with open(filepath, newline="") as f:
        for row in csv.reader(f):
            rows.append(row)
    for i in range(1, len(rows)):
        if len(rows[i]) > 5:
            name = rows[i][0].strip()
            if name in ("ols1", "ols2", "ols3", "ols4"):
                rows[i][5] = str(brisbane_rate)
            elif name in ("ols5", "ols6"):
                rows[i][5] = str(corridor_rate)
    with open(filepath, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def parse_dlexp_log(filepath):
    rows = []
    with open(filepath, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "iteration": int(r["iteration"]),
                "deadlock_found": int(r["deadlock_found"]),
                "deadlock_time": float(r["deadlock_time"]),
                "new_constraints": int(r["new_constraints"]),
                "total_constraints": int(r["total_constraints"]),
                "trains_completed": int(r["trains_completed"]),
                "trains_per_hr": float(r["trains_per_hr"]),
                "avg_wait_time": float(r.get("avg_wait_time", 0)),
                "avg_spawn_delay": float(r.get("avg_spawn_delay", 0)),
                "sim_run_time": float(r["sim_run_time"]),
            })
    return rows


def load_csv_log(filepath):
    if not os.path.exists(filepath):
        return [], set()
    rows = []
    with open(filepath, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    seeds = set()
    for r in rows:
        try:
            seeds.add(int(r["seed"]))
        except (KeyError, ValueError):
            pass
    return rows, seeds


def run_exe(workdir):
    stdout_path = os.path.join(workdir, "output", "exe_stdout.log")
    stderr_path = os.path.join(workdir, "output", "exe_stderr.log")
    with open(stdout_path, "w") as stdout_f, open(stderr_path, "w") as stderr_f:
        result = subprocess.run(
            [EXE_PATH],
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=stdout_f,
            stderr=stderr_f,
            timeout=EXE_TIMEOUT,
        )
    if result.returncode != 0:
        stderr_tail = ""
        try:
            with open(stderr_path) as f:
                stderr_tail = f.read()[-1000:]
        except OSError:
            pass
        raise RuntimeError(
            f"DesRail.exe failed (rc={result.returncode}) in {workdir}\n"
            f"stderr: {stderr_tail or '(empty)'}"
        )
    return result


def append_csv_row(filepath, header, row):
    write_header = not os.path.exists(filepath)
    with open(filepath, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerow(row)


# === Working directory ===

def setup_workdir(brisbane_rate, corridor_rate):
    label = rate_label(brisbane_rate, corridor_rate)
    workdir = os.path.join(SCRIPT_DIR, "work_tmba", label)
    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # Copy Toowoomba input files
    for fname in os.listdir(TMBA_INPUT):
        src = os.path.join(TMBA_INPUT, fname)
        if os.path.isfile(src) and fname.endswith(".csv"):
            shutil.copy2(src, os.path.join(input_dir, fname))

    # Blank signal_constraints.csv for training (no engineered constraints during learning)
    sc_path = os.path.join(input_dir, "signal_constraints.csv")
    with open(sc_path, "w", newline="") as f:
        f.write("str_id,segment / occ_limit,head,target,comment\n")

    # Set scenario-specific spawn rates
    set_toowoomba_rates(
        os.path.join(input_dir, "open_loop_spawners.csv"),
        brisbane_rate, corridor_rate)

    # Set horizon and warmup
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "sim_len", HORIZON)
    set_csv_option(runctrl, "warmup", WARMUP)
    set_csv_option(runctrl, "animate", 0)
    set_csv_option(runctrl, "screen_output", 0)
    set_csv_option(runctrl, "log_output", 0)

    # Set deadlock avoidance experiment
    main_opt = os.path.join(input_dir, "main_option.csv")
    set_csv_option(main_opt, "enter experiment type", "deadlock_avoidance_exp")

    # Configure dlexp options
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "tree")
    set_csv_option(dlexp_opts, "START_DEBUG", 99999)
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")
    set_csv_option(dlexp_opts, "LAST_CONSTRAINT", "")
    set_csv_option(dlexp_opts, "LOG_CONSTRAINT_FIRES", 0)
    set_csv_option(dlexp_opts, "VERIFY_SEG", 0)

    return workdir


def get_results_dir(brisbane_rate, corridor_rate):
    label = rate_label(brisbane_rate, corridor_rate)
    return os.path.join(SCRIPT_DIR, "results_tmba", label)


# === Training phase ===

def run_training(brisbane_rate, corridor_rate, workdir):
    label = rate_label(brisbane_rate, corridor_rate)
    rdir = get_results_dir(brisbane_rate, corridor_rate)
    os.makedirs(rdir, exist_ok=True)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(rdir, "training_log.csv")
    constraints_result = os.path.join(rdir, "constraints.csv")
    warm_file = os.path.join(workdir, "warm_constraints.csv")

    # Configure for training — PL constraints ON so learner discovers only
    # what's needed beyond passing loop signals
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    set_csv_option(runctrl, "pl_constraints", 1)

    # Resume state from existing logs
    existing_rows, completed_seeds = load_csv_log(training_log)

    # Restore warm-start file if needed
    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)

    # Determine consecutive clean count from end of existing log
    consecutive_clean = 0
    for row in reversed(existing_rows):
        if int(row["new_constraints"]) == 0:
            consecutive_clean += 1
        else:
            break

    seed = (max(completed_seeds) + 1) if completed_seeds else 1

    # Check if already complete
    total_constraints = int(existing_rows[-1]["total_constraints"]) if existing_rows else 0
    if consecutive_clean >= CLEAN_THRESHOLD:
        print(f"[{label}] Training already converged "
              f"({total_constraints}c, {consecutive_clean} clean seeds)")
        return total_constraints, seed - 1, "converged"

    if seed > MAX_SEEDS:
        print(f"[{label}] Training already hit seed limit ({total_constraints}c)")
        return total_constraints, seed - 1, "max_seeds"

    if total_constraints > MAX_CONSTRAINTS:
        print(f"[{label}] Training already hit constraint limit ({total_constraints}c)")
        return total_constraints, seed - 1, "max_constraints"

    training_seeds_count = sum(
        1 for r in existing_rows if int(r["new_constraints"]) > 0)

    while consecutive_clean < CLEAN_THRESHOLD and seed <= MAX_SEEDS:
        # Check constraint limit
        if total_constraints > MAX_CONSTRAINTS:
            print(f"[{label}] Constraint limit reached: {total_constraints} > {MAX_CONSTRAINTS}")
            break

        # Configure seed and warm-start
        set_csv_option(runctrl, "seed", seed)
        if os.path.exists(warm_file):
            set_csv_option(dlexp_opts, "WARM_START_FILE", "warm_constraints.csv")
        else:
            set_csv_option(dlexp_opts, "WARM_START_FILE", "")

        # Run simulation
        try:
            run_exe(workdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] ERROR at seed {seed}: {e}")
            break

        # Parse results
        log_path = os.path.join(output_dir, "dlexp_log.csv")
        if not os.path.exists(log_path):
            print(f"[{label}] ERROR: no dlexp_log.csv at seed {seed}")
            break

        results = parse_dlexp_log(log_path)
        if not results:
            print(f"[{label}] ERROR: empty dlexp_log.csv at seed {seed}")
            break

        new_constraints = sum(r["new_constraints"] for r in results)
        total_constraints = results[-1]["total_constraints"]
        iterations = len(results)
        trains_completed = results[-1]["trains_completed"]
        trains_per_hr = results[-1]["trains_per_hr"]
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

        # Append to training log
        append_csv_row(training_log,
            ["seed", "deadlock_found", "new_constraints", "total_constraints",
             "iterations", "trains_completed", "trains_per_hr", "cpu_time"],
            [seed, deadlock_found, new_constraints, total_constraints,
             iterations, trains_completed, f"{trains_per_hr:.4f}",
             f"{cpu_time:.3f}"])

        status = "CLEAN" if new_constraints == 0 else f"+{new_constraints}c"
        print(f"[{label}] seed {seed}: {status} "
              f"(total={total_constraints}c, "
              f"{consecutive_clean}/{CLEAN_THRESHOLD} clean)")

        seed += 1

    # Determine termination reason
    if consecutive_clean >= CLEAN_THRESHOLD:
        reason = "converged"
    elif seed > MAX_SEEDS:
        reason = "max_seeds"
    elif total_constraints > MAX_CONSTRAINTS:
        reason = "max_constraints"
    else:
        reason = "error"

    total_seeds = seed - 1
    print(f"[{label}] Training done: {total_constraints}c from "
          f"{training_seeds_count} training seeds, {total_seeds} total seeds "
          f"({reason})")
    return total_constraints, total_seeds, reason


# === Benchmark phase ===

def run_benchmark(brisbane_rate, corridor_rate, workdir):
    label = rate_label(brisbane_rate, corridor_rate)
    rdir = get_results_dir(brisbane_rate, corridor_rate)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(rdir, "benchmark.csv")
    warm_file = os.path.join(workdir, "warm_constraints.csv")
    constraints_result = os.path.join(rdir, "constraints.csv")

    # Ensure warm-start file exists for learned benchmark
    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)

    has_constraints = os.path.exists(warm_file)

    # Resume from existing benchmark
    _, completed_bench_seeds = load_csv_log(benchmark_path)

    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)

    bench_seeds = list(range(1, BENCHMARK_SEEDS + 1))

    print(f"[{label}] Benchmarking {len(bench_seeds)} seeds "
          f"({len(completed_bench_seeds)} already done)")

    for i, s in enumerate(bench_seeds):
        if s in completed_bench_seeds:
            continue

        set_csv_option(runctrl, "seed", s)

        # --- Hybrid run: PL constraints + learned constraints ---
        sc_path = os.path.join(input_dir, "signal_constraints.csv")
        with open(sc_path, "w", newline="") as f:
            f.write("str_id,segment / occ_limit,head,target,comment\n")
        set_csv_option(runctrl, "pl_constraints", 1)
        if has_constraints:
            set_csv_option(dlexp_opts, "WARM_START_FILE", "warm_constraints.csv")
        else:
            set_csv_option(dlexp_opts, "WARM_START_FILE", "")

        try:
            run_exe(workdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] BENCHMARK ERROR (learned) seed {s}: {e}")
            continue

        log_path = os.path.join(output_dir, "dlexp_log.csv")
        learned = parse_dlexp_log(log_path)

        # --- PL-only baseline run (no learned constraints) ---
        set_csv_option(runctrl, "pl_constraints", 1)
        with open(sc_path, "w", newline="") as f:
            f.write("str_id,segment / occ_limit,head,target,comment\n")
        set_csv_option(dlexp_opts, "WARM_START_FILE", "")

        try:
            run_exe(workdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] BENCHMARK ERROR (engineered) seed {s}: {e}")
            continue

        eng = parse_dlexp_log(log_path)

        if not learned or not eng:
            print(f"[{label}] BENCHMARK ERROR: empty results for seed {s}")
            continue

        append_csv_row(benchmark_path,
            ["seed",
             "throughput_hybrid", "cpu_hybrid", "hybrid_deadlocked",
             "avg_wait_hybrid", "avg_spawn_hybrid",
             "throughput_pl_only", "cpu_pl_only", "pl_only_deadlocked",
             "avg_wait_pl_only", "avg_spawn_pl_only"],
            [s,
             f"{learned[0]['trains_per_hr']:.4f}",
             f"{learned[0]['sim_run_time']:.3f}",
             learned[0]["deadlock_found"],
             f"{learned[0]['avg_wait_time']:.6f}",
             f"{learned[0]['avg_spawn_delay']:.6f}",
             f"{eng[0]['trains_per_hr']:.4f}",
             f"{eng[0]['sim_run_time']:.3f}",
             eng[0]["deadlock_found"],
             f"{eng[0]['avg_wait_time']:.6f}",
             f"{eng[0]['avg_spawn_delay']:.6f}"])

        dl_flag = " (PL-ONLY DEADLOCK!)" if eng[0]["deadlock_found"] else ""
        if (i + 1) % 10 == 0 or i == len(bench_seeds) - 1:
            print(f"[{label}] benchmark {i + 1}/{len(bench_seeds)}: "
                  f"hybrid={learned[0]['trains_per_hr']:.2f} "
                  f"pl_only={eng[0]['trains_per_hr']:.2f}{dl_flag}")

    print(f"[{label}] Benchmark complete")


# === Scenario runner ===

def run_scenario(brisbane_rate, corridor_rate, skip_training=False, skip_benchmark=False):
    label = rate_label(brisbane_rate, corridor_rate)
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"[{label}] Starting scenario")
    print(f"{'='*60}")

    workdir = setup_workdir(brisbane_rate, corridor_rate)

    if not skip_training:
        run_training(brisbane_rate, corridor_rate, workdir)
    if not skip_benchmark:
        run_benchmark(brisbane_rate, corridor_rate, workdir)

    elapsed = time.time() - t0
    print(f"[{label}] Scenario complete in {elapsed:.0f}s")
    return brisbane_rate, corridor_rate, elapsed


def run_scenario_wrapper(args):
    br, cr, skip_t, skip_b = args
    try:
        return run_scenario(br, cr, skip_training=skip_t, skip_benchmark=skip_b)
    except Exception as e:
        label = rate_label(br, cr)
        print(f"[{label}] FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        return br, cr, -1


# === Main ===

def parse_scenario_spec(spec):
    parts = spec.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid scenario spec '{spec}', expected 'b#.##:c#.##'")
    b_str, c_str = parts
    if not b_str.startswith("b"):
        raise ValueError(f"Invalid Brisbane rate '{b_str}', expected 'b#.##'")
    if not c_str.startswith("c"):
        raise ValueError(f"Invalid corridor rate '{c_str}', expected 'c#.##'")
    return float(b_str[1:]), float(c_str[1:])


def main():
    parser = argparse.ArgumentParser(
        description="Run Toowoomba deadlock avoidance experiments")
    parser.add_argument("--jobs", "-j", type=int, default=1,
                        help="Number of parallel scenario workers (default: 1)")
    parser.add_argument("--scenarios", nargs="+", metavar="b#.##:c#.##",
                        help="Run specific scenarios (e.g., b0.75:c0.50)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print scenarios without running")
    parser.add_argument("--skip-training", action="store_true",
                        help="Skip training, benchmark only")
    parser.add_argument("--skip-benchmark", action="store_true",
                        help="Skip benchmark, training only")
    args = parser.parse_args()

    if not os.path.exists(EXE_PATH):
        print(f"ERROR: DesRail.exe not found at {EXE_PATH}")
        print("Copy DesRail.exe to x64/Release/ or build the C++ simulator.")
        sys.exit(1)

    # Build scenario list
    if args.scenarios:
        scenarios = [parse_scenario_spec(s) for s in args.scenarios]
    else:
        scenarios = [(br, cr)
                     for br in BRISBANE_RATES
                     for cr in CORRIDOR_RATES]

    print(f"Toowoomba experiment campaign: {len(scenarios)} scenarios, "
          f"{args.jobs} parallel workers")
    print(f"Brisbane rates: {BRISBANE_RATES}")
    print(f"Corridor rates: {CORRIDOR_RATES}")
    print(f"Horizon: {HORIZON}h (warmup: {WARMUP}h)")
    print(f"Termination: converge={CLEAN_THRESHOLD} clean seeds, "
          f"OR seeds>{MAX_SEEDS}, OR constraints>{MAX_CONSTRAINTS}")

    if args.dry_run:
        for br, cr in scenarios:
            print(f"  {rate_label(br, cr)}")
        return

    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(run_scenario_wrapper,
                       (br, cr, args.skip_training, args.skip_benchmark)): (br, cr)
                       for br, cr in scenarios}
            for fut in as_completed(futures):
                br, cr, elapsed = fut.result()
                label = rate_label(br, cr)
                if elapsed < 0:
                    print(f"\n*** [{label}] FAILED ***")
                else:
                    print(f"\n*** [{label}] DONE in {elapsed:.0f}s ***")
    else:
        for br, cr in scenarios:
            run_scenario_wrapper((br, cr, args.skip_training, args.skip_benchmark))

    print("\nAll scenarios complete.")


if __name__ == "__main__":
    main()
