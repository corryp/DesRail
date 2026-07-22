#!/usr/bin/env python3
"""
Experiment campaign: synthetic corridor deadlock avoidance.

Runs training and benchmark phases across multiple corridor sizes (N)
and spawn rates, comparing learned constraints vs hand-crafted PL constraints.

Usage:
    python run_experiments.py                          # Run all 45 scenarios
    python run_experiments.py --jobs 4                 # 4 scenarios in parallel
    python run_experiments.py --scenarios N02:r2.00    # Single scenario
    python run_experiments.py --scenarios N10:r1.75 N10:r2.00  # Multiple
    python run_experiments.py --dry-run                # Show what would run
    python run_experiments.py --skip-benchmark         # Training only
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
    """Walk up to the repo root (the dir holding DesRail/). Honors $DESRAIL_REPO."""
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
    return os.path.dirname(SCRIPT_DIR)


REPO_ROOT = _find_repo_root(SCRIPT_DIR)
EXE_PATH = os.path.join(REPO_ROOT, "x64", "Release", "DesRail.exe")
GENERATOR_SCRIPT = os.path.join(REPO_ROOT, "DesRail", "generate_corridor.py")

# === Experiment parameters ===

CORRIDOR_SIZES = [2, 5, 10, 15, 20]
SPAWN_RATES = [1.0, 1.25, 1.50, 1.75, 2.0, 2.25, 2.50, 2.75, 3.0]
WARMUP = 24        # hours
HORIZON = 168 + WARMUP  # sim_len = 1 week collection + warmup
CORRIDOR_LEN = 2.0
CORRIDOR_LEN_VAR = 0.5  # uniform(1.5, 2.5) km between passing loops
LOOP_LEN = 0.7
CLEAN_THRESHOLD = 100
MAX_ITERATIONS_TRAINING = 2000
MAX_SEEDS = 5000  # Safety cap to prevent infinite loops
EXE_TIMEOUT = 28800  # 8 hours max per exe invocation


# === Helpers ===

def rate_label(rate):
    """Format rate for directory/file names: 1.0 -> 'r1.00'."""
    return f"r{rate:.2f}"


def scenario_label(N, rate):
    return f"N{N:02d}/{rate_label(rate)}"


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


# Big-M horizon for drained runs: spawning stops at the stated horizon
# (start_warmdown) and the run continues to DRAIN_HORIZON so a late-forming
# deadlock gets its full detection window (the DeadlockMonitor early-stops the
# moment the network clears, so safe runs terminate promptly regardless).
DRAIN_HORIZON = 1000.0


def apply_drain(runctrl_path, horizon=None):
    """Enable the drain on a work-dir runctrl. Sets start_warmdown to the stated
    horizon (defaults to the file's current sim_len) and extends sim_len to the
    big-M value. Idempotent; the stats window stays [warmup, horizon] so reported
    throughput is unchanged. Both learner training and eval use this."""
    rows = []
    with open(runctrl_path, newline="") as f:
        for row in csv.reader(f):
            rows.append(row)
    cur_sim_len = None
    for row in rows:
        if row and row[0].strip() == "sim_len":
            cur_sim_len = float(row[1])
            break
    if horizon is None:
        if cur_sim_len is None:
            raise ValueError(f"apply_drain: no sim_len in {runctrl_path} and no horizon given")
        # don't re-read an already-drained file's big-M as the horizon
        horizon = cur_sim_len if cur_sim_len < DRAIN_HORIZON else None
        if horizon is None:
            # already drained: keep the existing start_warmdown
            for row in rows:
                if row and row[0].strip() == "start_warmdown":
                    horizon = float(row[1]); break
            if horizon is None:
                raise ValueError(f"apply_drain: {runctrl_path} has big-M sim_len but no start_warmdown")
    set_or_add_csv_option(runctrl_path, "sim_len", DRAIN_HORIZON)
    set_or_add_csv_option(runctrl_path, "start_warmdown", horizon)
    return horizon


def set_or_add_csv_option(filepath, option_name, value):
    """Like set_csv_option but appends the row if the option is absent."""
    rows = []
    with open(filepath, newline="") as f:
        for row in csv.reader(f):
            rows.append(row)
    for row in rows:
        if row and row[0].strip() == option_name:
            row[1] = str(value)
            break
    else:
        rows.append([option_name, str(value), ""])
    with open(filepath, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def set_spawn_rate(filepath, rate):
    """Set dist_prm1 (column 5) for all data rows in open_loop_spawners.csv."""
    rows = []
    with open(filepath, newline="") as f:
        for row in csv.reader(f):
            rows.append(row)

    for i in range(1, len(rows)):
        if len(rows[i]) > 5:
            rows[i][5] = str(rate)

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
    """Run DesRail.exe with the given working directory. Returns subprocess result."""
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


# === Config generation ===

def generate_base_configs():
    """Generate base network configs for each corridor size."""
    configs_dir = os.path.join(SCRIPT_DIR, "configs")
    for N in CORRIDOR_SIZES:
        config_dir = os.path.join(configs_dir, f"N{N:02d}")
        marker = os.path.join(config_dir, "input", "network.csv")
        if os.path.exists(marker):
            print(f"  N={N}: configs exist, skipping")
            continue

        subprocess.run(
            [sys.executable, GENERATOR_SCRIPT,
             "--num_loops", str(N),
             "--output_dir", config_dir,
             "--corridor_len", str(CORRIDOR_LEN),
             "--corridor_len_var", str(CORRIDOR_LEN_VAR),
             "--loop_len", str(LOOP_LEN),
             "--spawn_rate", "1.0",  # placeholder, overridden per scenario
             "--horizon", str(HORIZON),
             "--warmup", str(WARMUP),
             "--seed", "1"],
            check=True,
            capture_output=True,
        )
        print(f"  N={N}: generated")


# === Working directory ===

def setup_workdir(N, rate):
    """Create/update working directory for a scenario."""
    workdir = os.path.join(SCRIPT_DIR, "work", f"N{N:02d}", rate_label(rate))
    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # Copy base configs
    base_input = os.path.join(SCRIPT_DIR, "configs", f"N{N:02d}", "input")
    for fname in os.listdir(base_input):
        src = os.path.join(base_input, fname)
        dst = os.path.join(input_dir, fname)
        if os.path.isfile(src):
            shutil.copy2(src, dst)

    # Set scenario-specific spawn rate
    set_spawn_rate(os.path.join(input_dir, "open_loop_spawners.csv"), rate)

    return workdir


def get_results_dir(N, rate):
    """Return path to the results directory for a scenario."""
    return os.path.join(SCRIPT_DIR, "results", f"N{N:02d}", rate_label(rate))


# === Training phase ===

def run_training(N, rate, workdir):
    """Run the iterative training phase for one scenario.

    Returns (final_constraint_count, total_seeds_run, training_seeds).
    """
    label = scenario_label(N, rate)
    rdir = get_results_dir(N, rate)
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
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "tree")
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
            # Update union constraint file
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

def run_benchmark(N, rate, workdir):
    """Run benchmark comparing learned vs PL constraints on fixed seed range."""
    label = scenario_label(N, rate)
    rdir = get_results_dir(N, rate)

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

    # Fixed seed range: 1..CLEAN_THRESHOLD, capped at max training seed
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
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "tree")

    has_constraints = os.path.exists(warm_file)

    print(f"[{label}] Benchmarking {len(bench_seeds)} seeds "
          f"({len(completed_bench_seeds)} already done)")

    for i, s in enumerate(bench_seeds):
        if s in completed_bench_seeds:
            continue

        set_csv_option(runctrl, "seed", s)

        # --- Learned constraints run ---
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

        # --- PL constraints run ---
        set_csv_option(runctrl, "pl_constraints", 1)
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

        dl_flag = " (PL DEADLOCK!)" if pl[0]["deadlock_found"] else ""
        if (i + 1) % 10 == 0 or i == len(bench_seeds) - 1:
            print(f"[{label}] benchmark {i + 1}/{len(bench_seeds)}: "
                  f"learned={learned[0]['trains_per_hr']:.2f} "
                  f"PL={pl[0]['trains_per_hr']:.2f}{dl_flag}")

    print(f"[{label}] Benchmark complete")


# === Scenario runner ===

def run_scenario(N, rate):
    """Run full training + benchmark for one (N, rate) scenario."""
    label = scenario_label(N, rate)
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"[{label}] Starting scenario")
    print(f"{'='*60}")

    workdir = setup_workdir(N, rate)
    run_training(N, rate, workdir)
    run_benchmark(N, rate, workdir)

    elapsed = time.time() - t0
    print(f"[{label}] Scenario complete in {elapsed:.0f}s")
    return N, rate, elapsed


def run_scenario_wrapper(args):
    """Wrapper for ProcessPoolExecutor (unpacks tuple)."""
    N, rate = args
    try:
        return run_scenario(N, rate)
    except Exception as e:
        label = scenario_label(N, rate)
        print(f"[{label}] FATAL ERROR: {e}")
        return N, rate, -1


def run_scenario_training_only(N, rate):
    """Run training phase only (no benchmark)."""
    label = scenario_label(N, rate)
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"[{label}] Starting scenario (training only)")
    print(f"{'='*60}")

    workdir = setup_workdir(N, rate)
    run_training(N, rate, workdir)

    elapsed = time.time() - t0
    print(f"[{label}] Training complete in {elapsed:.0f}s")
    return N, rate, elapsed


def run_scenario_training_only_wrapper(args):
    N, rate = args
    try:
        return run_scenario_training_only(N, rate)
    except Exception as e:
        label = scenario_label(N, rate)
        print(f"[{label}] FATAL ERROR: {e}")
        return N, rate, -1


# === Main ===

def parse_scenario_spec(spec):
    """Parse 'N10:r2.00' into (10, 2.0)."""
    parts = spec.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid scenario spec '{spec}', expected 'N##:r#.##'")
    n_str, r_str = parts
    if not n_str.startswith("N"):
        raise ValueError(f"Invalid N spec '{n_str}', expected 'N##'")
    if not r_str.startswith("r"):
        raise ValueError(f"Invalid rate spec '{r_str}', expected 'r#.##'")
    return int(n_str[1:]), float(r_str[1:])


def main():
    parser = argparse.ArgumentParser(
        description="Run synthetic corridor deadlock avoidance experiments")
    parser.add_argument("--jobs", "-j", type=int, default=1,
                        help="Number of parallel scenario workers (default: 1)")
    parser.add_argument("--scenarios", nargs="+", metavar="N##:r#.##",
                        help="Run specific scenarios (e.g., N10:r2.00)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print scenarios without running")
    parser.add_argument("--skip-benchmark", action="store_true",
                        help="Run training phase only")
    parser.add_argument("--skip-generate", action="store_true",
                        help="Skip base config generation")
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
        scenarios = [(N, rate) for N in CORRIDOR_SIZES for rate in SPAWN_RATES]

    print(f"Experiment campaign: {len(scenarios)} scenarios, "
          f"{args.jobs} parallel workers")

    if args.dry_run:
        for N, rate in scenarios:
            print(f"  {scenario_label(N, rate)}")
        return

    # Generate base configs
    if not args.skip_generate:
        print("\nGenerating base configs...")
        generate_base_configs()

    # Select runner
    if args.skip_benchmark:
        wrapper = run_scenario_training_only_wrapper
    else:
        wrapper = run_scenario_wrapper

    # Run scenarios
    t0 = time.time()
    if args.jobs <= 1:
        for N, rate in scenarios:
            wrapper((N, rate))
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(wrapper, (N, rate)): (N, rate)
                       for N, rate in scenarios}
            for future in as_completed(futures):
                N, rate, elapsed = future.result()
                if elapsed >= 0:
                    print(f"[{scenario_label(N, rate)}] DONE ({elapsed:.0f}s)")

    total = time.time() - t0
    print(f"\nAll scenarios complete in {total:.0f}s ({total/3600:.1f}h)")


if __name__ == "__main__":
    main()
