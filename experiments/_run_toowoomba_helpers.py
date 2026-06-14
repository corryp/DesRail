#!/usr/bin/env python3
"""
Experiment campaign: Toowoomba deadlock avoidance.

Runs training and benchmark phases across a grid of spawn rates for the
Toowoomba network, varying Brisbane route rates (ols1-4) and corridor
route rates (ols5-6) independently.

Usage:
    python run_toowoomba.py                          # Run all 12 scenarios
    python run_toowoomba.py --jobs 12                # 12 parallel workers
    python run_toowoomba.py --scenarios b0.75:c0.50  # Single scenario
    python run_toowoomba.py --dry-run                # Show what would run
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


def _find_repo_root(start):
    """Walk up looking for a dir with DesRail/ + x64/. Honors $DESRAIL_REPO."""
    override = os.environ.get("DESRAIL_REPO")
    if override:
        return override
    d = start
    for _ in range(6):
        if os.path.isdir(os.path.join(d, "DesRail")) and os.path.isdir(os.path.join(d, "x64")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.dirname(SCRIPT_DIR)


REPO_ROOT = _find_repo_root(SCRIPT_DIR)
EXE_PATH = os.path.join(REPO_ROOT, "x64", "Release", "DesRail.exe")
TMBA_INPUT = os.path.join(REPO_ROOT, "DesRail", "input")
MANUAL_CONSTRAINTS = os.path.join(SCRIPT_DIR, "tmba_manual_constraints.csv")

# === Experiment parameters ===

# Brisbane routes: ols1-4 (rows 1-4 in CSV, 0-indexed after header)
BRISBANE_RATES = [0.25, 0.75, 1.25, 1.75]
# Corridor routes: ols5-6 (rows 5-6 in CSV)
CORRIDOR_RATES = [0.25, 0.50, 0.75]

WARMUP = 24        # hours
HORIZON = 168 + WARMUP  # 1 week collection + warmup
CLEAN_THRESHOLD = 100
MAX_ITERATIONS_TRAINING = 2000
MAX_SEEDS = 5000
EXE_TIMEOUT = 28800  # 8 hours max per exe invocation


# === Helpers ===

def rate_label(brisbane_rate, corridor_rate):
    """Format rates for directory names: 'b0.75_c0.50'."""
    return f"b{brisbane_rate:.2f}_c{corridor_rate:.2f}"


def scenario_label(brisbane_rate, corridor_rate):
    return rate_label(brisbane_rate, corridor_rate)


def set_csv_option(filepath, option_name, value):
    """Set a value in a CSV config file where column 0 is the option name."""
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
    """Set spawn rates in open_loop_spawners.csv.

    ols1-4 (rows 1-4): Brisbane routes -> brisbane_rate
    ols5-6 (rows 5-6): Corridor routes -> corridor_rate
    """
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
    """Parse output/dlexp_log.csv into a list of dicts."""
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
    """Load a CSV log file, return (list of row dicts, set of seed ints)."""
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
    """Run DesRail.exe with the given working directory."""
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
    """Append a row to a CSV file, writing header if file doesn't exist."""
    write_header = not os.path.exists(filepath)
    with open(filepath, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerow(row)


# === Working directory ===

def setup_workdir(brisbane_rate, corridor_rate):
    """Create/update working directory for a scenario."""
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

    # Blank signal_constraints.csv for training (manual constraints saved separately)
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

    return workdir


def get_results_dir(brisbane_rate, corridor_rate):
    """Return path to the results directory for a scenario."""
    label = rate_label(brisbane_rate, corridor_rate)
    return os.path.join(SCRIPT_DIR, "results_tmba", label)


# === Training phase ===

def run_training(brisbane_rate, corridor_rate, workdir):
    """Run the iterative training phase for one scenario."""
    label = scenario_label(brisbane_rate, corridor_rate)
    rdir = get_results_dir(brisbane_rate, corridor_rate)
    os.makedirs(rdir, exist_ok=True)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(rdir, "training_log.csv")
    constraints_result = os.path.join(rdir, "constraints.csv")
    warm_file = os.path.join(workdir, "warm_constraints.csv")

    # Configure for training
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    set_csv_option(runctrl, "pl_constraints", 0)

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

    if consecutive_clean >= CLEAN_THRESHOLD:
        total_c = int(existing_rows[-1]["total_constraints"]) if existing_rows else 0
        print(f"[{label}] Training already complete "
              f"({total_c}c, {consecutive_clean} clean seeds)")
        return total_c, seed - 1, sum(
            1 for r in existing_rows if int(r["new_constraints"]) > 0)

    total_constraints = 0
    training_seeds_count = sum(
        1 for r in existing_rows if int(r["new_constraints"]) > 0)

    while consecutive_clean < CLEAN_THRESHOLD and seed <= MAX_SEEDS:
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

    if seed > MAX_SEEDS:
        print(f"[{label}] WARNING: hit MAX_SEEDS={MAX_SEEDS} without convergence")

    total_seeds = seed - 1
    print(f"[{label}] Training done: {total_constraints}c from "
          f"{training_seeds_count} training seeds, {total_seeds} total seeds")
    return total_constraints, total_seeds, training_seeds_count


# === Benchmark phase ===

def run_benchmark(brisbane_rate, corridor_rate, workdir):
    """Run benchmark comparing learned vs PL constraints on fixed seed range."""
    label = scenario_label(brisbane_rate, corridor_rate)
    rdir = get_results_dir(brisbane_rate, corridor_rate)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(rdir, "training_log.csv")
    benchmark_path = os.path.join(rdir, "benchmark.csv")
    warm_file = os.path.join(workdir, "warm_constraints.csv")
    constraints_result = os.path.join(rdir, "constraints.csv")

    # Ensure warm-start file exists
    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)

    # Fixed seed range
    all_training, _ = load_csv_log(training_log)
    if not all_training:
        print(f"[{label}] No training data, skipping benchmark")
        return
    max_training_seed = max(int(row["seed"]) for row in all_training)
    max_bench = min(CLEAN_THRESHOLD, max_training_seed)
    bench_seeds = list(range(1, max_bench + 1))

    if max_bench < CLEAN_THRESHOLD:
        print(f"[{label}] WARNING: training only reached seed {max_training_seed}, "
              f"benchmarking {max_bench} seeds (target {CLEAN_THRESHOLD})")

    # Resume from existing benchmark
    _, completed_bench_seeds = load_csv_log(benchmark_path)

    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)

    has_constraints = os.path.exists(warm_file)

    print(f"[{label}] Benchmarking {len(bench_seeds)} seeds "
          f"({len(completed_bench_seeds)} already done)")

    for i, s in enumerate(bench_seeds):
        if s in completed_bench_seeds:
            continue

        set_csv_option(runctrl, "seed", s)

        # --- Learned constraints run ---
        # Blank manual signal constraints
        sc_path = os.path.join(input_dir, "signal_constraints.csv")
        with open(sc_path, "w", newline="") as f:
            f.write("str_id,segment / occ_limit,head,target,comment\n")
        set_csv_option(runctrl, "pl_constraints", 0)
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

        # --- Manual constraints run ---
        set_csv_option(runctrl, "pl_constraints", 1)
        # Load hand-crafted signal constraints
        sc_path = os.path.join(input_dir, "signal_constraints.csv")
        if os.path.exists(MANUAL_CONSTRAINTS):
            shutil.copy2(MANUAL_CONSTRAINTS, sc_path)
        set_csv_option(dlexp_opts, "WARM_START_FILE", "")

        try:
            run_exe(workdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] BENCHMARK ERROR (PL) seed {s}: {e}")
            continue

        pl = parse_dlexp_log(log_path)

        if not learned or not pl:
            print(f"[{label}] BENCHMARK ERROR: empty results for seed {s}")
            continue

        append_csv_row(benchmark_path,
            ["seed", "throughput_learned", "cpu_learned", "learned_deadlocked",
             "avg_wait_learned", "avg_spawn_learned",
             "throughput_manual", "cpu_manual", "manual_deadlocked",
             "avg_wait_manual", "avg_spawn_manual"],
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

        dl_flag = " (MANUAL DEADLOCK!)" if pl[0]["deadlock_found"] else ""
        if (i + 1) % 10 == 0 or i == len(bench_seeds) - 1:
            print(f"[{label}] benchmark {i + 1}/{len(bench_seeds)}: "
                  f"learned={learned[0]['trains_per_hr']:.2f} "
                  f"manual={pl[0]['trains_per_hr']:.2f}{dl_flag}")

    print(f"[{label}] Benchmark complete")


# === Scenario runner ===

def run_scenario(brisbane_rate, corridor_rate):
    """Run full training + benchmark for one scenario."""
    label = scenario_label(brisbane_rate, corridor_rate)
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"[{label}] Starting scenario")
    print(f"{'='*60}")

    workdir = setup_workdir(brisbane_rate, corridor_rate)
    run_training(brisbane_rate, corridor_rate, workdir)
    run_benchmark(brisbane_rate, corridor_rate, workdir)

    elapsed = time.time() - t0
    print(f"[{label}] Scenario complete in {elapsed:.0f}s")
    return brisbane_rate, corridor_rate, elapsed


def run_scenario_wrapper(args):
    """Wrapper for ProcessPoolExecutor."""
    br, cr = args
    try:
        return run_scenario(br, cr)
    except Exception as e:
        label = scenario_label(br, cr)
        print(f"[{label}] FATAL ERROR: {e}")
        return br, cr, -1


def run_scenario_training_only(brisbane_rate, corridor_rate):
    """Run training phase only."""
    label = scenario_label(brisbane_rate, corridor_rate)
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"[{label}] Starting scenario (training only)")
    print(f"{'='*60}")

    workdir = setup_workdir(brisbane_rate, corridor_rate)
    run_training(brisbane_rate, corridor_rate, workdir)

    elapsed = time.time() - t0
    print(f"[{label}] Training complete in {elapsed:.0f}s")
    return brisbane_rate, corridor_rate, elapsed


def run_scenario_training_only_wrapper(args):
    br, cr = args
    try:
        return run_scenario_training_only(br, cr)
    except Exception as e:
        label = scenario_label(br, cr)
        print(f"[{label}] FATAL ERROR: {e}")
        return br, cr, -1


# === Main ===

def parse_scenario_spec(spec):
    """Parse 'b0.75:c0.50' into (0.75, 0.50)."""
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
    parser.add_argument("--skip-benchmark", action="store_true",
                        help="Run training phase only")
    args = parser.parse_args()

    # Verify exe exists
    if not os.path.exists(EXE_PATH):
        print(f"ERROR: DesRail.exe not found at {EXE_PATH}")
        print("Build the C++ simulator first (Release|x64).")
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

    if args.dry_run:
        for br, cr in scenarios:
            print(f"  {scenario_label(br, cr)}")
        return

    worker = (run_scenario_training_only_wrapper if args.skip_benchmark
              else run_scenario_wrapper)

    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(worker, (br, cr)): (br, cr)
                       for br, cr in scenarios}
            for fut in as_completed(futures):
                br, cr, elapsed = fut.result()
                label = scenario_label(br, cr)
                if elapsed < 0:
                    print(f"\n*** [{label}] FAILED ***")
                else:
                    print(f"\n*** [{label}] DONE in {elapsed:.0f}s ***")
    else:
        for br, cr in scenarios:
            worker((br, cr))

    print("\nAll scenarios complete.")


if __name__ == "__main__":
    main()
