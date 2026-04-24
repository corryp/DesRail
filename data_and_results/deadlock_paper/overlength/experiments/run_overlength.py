#!/usr/bin/env python3
"""
Overlength train experiment campaign.

Tests the hybrid approach on corridors with mixed short/overlength trains.
For each scenario (N, fraction):
  1. PL benchmark   — PL + max_length constraints only (no engineered spawn-hold),
                      100 seeds, count deadlocks
  2. Training       — PL + max_length on, learner discovers remaining constraints
  3. Hybrid bench   — PL + max_length + learned constraints, 100 seeds
  4. Engineered bench — PL + max_length + engineered spawn-hold constraints, 100 seeds

Train lengths: freight = 0.44 km (fits in 0.7 km siding),
               freight_long = 0.80 km (does NOT fit).

Termination: converge (100 clean seeds) OR seeds > 3,000 OR constraints > 25,000.

Usage:
    python run_overlength.py                          # All scenarios
    python run_overlength.py --jobs 4                 # 4 parallel workers
    python run_overlength.py --num-loops 5 10         # Only N=5,10
    python run_overlength.py --fractions 0.25         # Only f=0.25
    python run_overlength.py --dry-run
    python run_overlength.py --skip-training          # Benchmarks only
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
GENERATOR_SCRIPT = os.path.join(REPO_ROOT, "DesRail", "generate_corridor.py")
CONSTRAINT_GEN_SCRIPT = os.path.join(REPO_ROOT, "DesRail", "generate_overlength_constraints.py")

# === Experiment parameters ===

NUM_LOOPS = [2, 5, 10, 15, 20]
RATE = 2.00
FRACTIONS = [0.10, 0.25, 0.50]

WARMUP = 24
HORIZON = 168 + WARMUP
CORRIDOR_LEN = 2.0
CORRIDOR_LEN_VAR = 0.5
LOOP_LEN = 0.7
PL_MAX_LENGTH = 0.0  # max_length no longer needed on PL constraints after is_violated_seg bug fix
MIN_LENGTH = 0.8        # overlength threshold for engineered constraints

CLEAN_THRESHOLD = 100
MAX_ITERATIONS_TRAINING = 2000
MAX_SEEDS = 3000
MAX_CONSTRAINTS = 25000
BENCHMARK_SEEDS = 100
EXE_TIMEOUT = 28800


# === Helpers ===

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
        kwargs = dict(
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=stdout_f,
            stderr=stderr_f,
            timeout=EXE_TIMEOUT,
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run([EXE_PATH], **kwargs)
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


def append_csv_row(filepath, header, row):
    write_header = not os.path.exists(filepath)
    with open(filepath, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerow(row)


# === Labels and paths ===

def scenario_label(N, fraction):
    return f"N{N:02d}/f{fraction:.2f}"


def get_results_dir(N, fraction):
    return os.path.join(SCRIPT_DIR, "results_overlength",
                        f"N{N:02d}", f"f{fraction:.2f}")


# === Config generation ===

def generate_base_configs(N):
    """Generate corridor configs with --overlength for this N."""
    config_dir = os.path.join(SCRIPT_DIR, "configs", f"N{N:02d}")
    marker = os.path.join(config_dir, "input", "network.csv")
    if os.path.exists(marker):
        return config_dir

    subprocess.run(
        [sys.executable, GENERATOR_SCRIPT,
         "--num_loops", str(N),
         "--output_dir", config_dir,
         "--corridor_len", str(CORRIDOR_LEN),
         "--corridor_len_var", str(CORRIDOR_LEN_VAR),
         "--loop_len", str(LOOP_LEN),
         "--spawn_rate", str(RATE),
         "--horizon", str(HORIZON),
         "--warmup", str(WARMUP),
         "--seed", "1",
         "--overlength",
         "--pl_max_length", str(PL_MAX_LENGTH)],
        check=True, capture_output=True,
    )
    print(f"  N={N}: generated corridor configs")
    return config_dir


def generate_engineered_constraints(N, output_path):
    """Generate spawn-hold overlength constraints for N-loop corridor."""
    subprocess.run(
        [sys.executable, CONSTRAINT_GEN_SCRIPT,
         "-n", str(N), "-o", output_path,
         "--min_length", str(MIN_LENGTH)],
        check=True, capture_output=True,
    )


# === Spawner setup ===

def write_overlength_spawners(filepath, base_spawners_path, rate, fraction):
    """Read base spawners and set rates based on fraction split."""
    header = None
    rows = []
    with open(base_spawners_path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        for row in reader:
            if row:
                rows.append(row)

    for row in rows:
        template = row[3]  # train_template column
        if "long" in template.lower():
            row[5] = str(rate * fraction) if fraction > 0 else str(0)
        else:
            row[5] = str(rate * (1.0 - fraction)) if fraction < 1.0 else str(0)

    # Remove zero-rate spawners
    rows = [r for r in rows if float(r[5]) > 0]

    with open(filepath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow(row)


# === Working directory ===

def setup_workdir(N, fraction, config_dir):
    """Create working directory for a scenario."""
    workdir = os.path.join(SCRIPT_DIR, "work", f"N{N:02d}", f"f{fraction:.2f}")
    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # Copy base configs
    base_input = os.path.join(config_dir, "input")
    for fname in os.listdir(base_input):
        src = os.path.join(base_input, fname)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(input_dir, fname))

    # Write scenario-specific spawner rates
    write_overlength_spawners(
        os.path.join(input_dir, "open_loop_spawners.csv"),
        os.path.join(base_input, "open_loop_spawners.csv"),
        RATE, fraction)

    # Configure dlexp options
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp_opts, "START_DEBUG", 99999)
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")
    set_csv_option(dlexp_opts, "LAST_CONSTRAINT", "")
    set_csv_option(dlexp_opts, "LOG_CONSTRAINT_FIRES", 0)

    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "animate", 0)
    set_csv_option(runctrl, "screen_output", 0)
    set_csv_option(runctrl, "log_output", 0)

    # Remove stale warm_constraints from previous runs
    warm_file = os.path.join(workdir, "warm_constraints.csv")
    if os.path.exists(warm_file):
        os.remove(warm_file)

    return workdir


# === Phase 1: PL Benchmark (PL + max_length only, no engineered spawn-hold) ===

def run_pl_benchmark(N, fraction, workdir):
    label = scenario_label(N, fraction)
    rdir = get_results_dir(N, fraction)
    os.makedirs(rdir, exist_ok=True)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(rdir, "pl_benchmark.csv")

    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")

    # Remove engineered constraints file so only PL constraints apply
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)

    _, completed_seeds = load_csv_log(benchmark_path)
    bench_seeds = list(range(1, BENCHMARK_SEEDS + 1))

    print(f"[{label}] PL benchmark: {len(bench_seeds)} seeds "
          f"({len(completed_seeds)} done)")

    existing_rows, _ = load_csv_log(benchmark_path)
    deadlock_count = sum(1 for r in existing_rows if int(r.get("deadlocked", 0)))

    for i, s in enumerate(bench_seeds):
        if s in completed_seeds:
            continue
        set_csv_option(runctrl, "seed", s)
        try:
            run_exe(workdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] PL ERROR seed {s}: {e}")
            continue

        log_path = os.path.join(output_dir, "dlexp_log.csv")
        results = parse_dlexp_log(log_path)
        if not results:
            continue

        r = results[0]
        if r["deadlock_found"]:
            deadlock_count += 1

        append_csv_row(benchmark_path,
            ["seed", "throughput", "cpu_time", "deadlocked",
             "avg_wait_time", "avg_spawn_delay"],
            [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
             r["deadlock_found"],
             f"{r['avg_wait_time']:.6f}", f"{r['avg_spawn_delay']:.6f}"])

        if (i + 1) % 10 == 0 or i == len(bench_seeds) - 1:
            print(f"[{label}] PL bench {i+1}/{len(bench_seeds)}: "
                  f"{deadlock_count} DL")

    print(f"[{label}] PL benchmark: {deadlock_count}/{len(bench_seeds)} deadlocks")
    return deadlock_count


# === Phase 2: Training (PL + max_length on, learn residual constraints) ===

def run_training(N, fraction, workdir):
    label = scenario_label(N, fraction)
    rdir = get_results_dir(N, fraction)
    os.makedirs(rdir, exist_ok=True)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(rdir, "training_log.csv")
    constraints_result = os.path.join(rdir, "constraints.csv")
    warm_file = os.path.join(workdir, "warm_constraints.csv")

    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    set_csv_option(runctrl, "pl_constraints", 1)

    # Remove engineered constraints — learner must discover them
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)

    # Resume state
    existing_rows, completed_seeds = load_csv_log(training_log)

    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)

    consecutive_clean = 0
    for row in reversed(existing_rows):
        if int(row["new_constraints"]) == 0:
            consecutive_clean += 1
        else:
            break

    seed = (max(completed_seeds) + 1) if completed_seeds else 1
    total_constraints = int(existing_rows[-1]["total_constraints"]) if existing_rows else 0

    # Check if already done
    if consecutive_clean >= CLEAN_THRESHOLD:
        print(f"[{label}] Training already converged ({total_constraints}c)")
        return total_constraints, "converged"
    if seed > MAX_SEEDS:
        print(f"[{label}] Training already hit seed limit ({total_constraints}c)")
        return total_constraints, "max_seeds"
    if total_constraints > MAX_CONSTRAINTS:
        print(f"[{label}] Training already hit constraint limit ({total_constraints}c)")
        return total_constraints, "max_constraints"

    while consecutive_clean < CLEAN_THRESHOLD and seed <= MAX_SEEDS:
        if total_constraints > MAX_CONSTRAINTS:
            print(f"[{label}] Constraint limit: {total_constraints} > {MAX_CONSTRAINTS}")
            break

        set_csv_option(runctrl, "seed", seed)
        if os.path.exists(warm_file):
            set_csv_option(dlexp_opts, "WARM_START_FILE", "warm_constraints.csv")
        else:
            set_csv_option(dlexp_opts, "WARM_START_FILE", "")

        try:
            run_exe(workdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] ERROR at seed {seed}: {e}")
            break

        log_path = os.path.join(output_dir, "dlexp_log.csv")
        if not os.path.exists(log_path):
            break

        results = parse_dlexp_log(log_path)
        if not results:
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
        else:
            consecutive_clean += 1

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

    if consecutive_clean >= CLEAN_THRESHOLD:
        reason = "converged"
    elif seed > MAX_SEEDS:
        reason = "max_seeds"
    elif total_constraints > MAX_CONSTRAINTS:
        reason = "max_constraints"
    else:
        reason = "error"

    print(f"[{label}] Training: {total_constraints}c, {seed-1} seeds ({reason})")
    return total_constraints, reason


# === Phase 3: Hybrid Benchmark (PL + learned) ===

def run_hybrid_benchmark(N, fraction, workdir):
    label = scenario_label(N, fraction)
    rdir = get_results_dir(N, fraction)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(rdir, "hybrid_benchmark.csv")
    warm_file = os.path.join(workdir, "warm_constraints.csv")
    constraints_result = os.path.join(rdir, "constraints.csv")

    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)

    if not os.path.exists(warm_file):
        print(f"[{label}] No learned constraints, skipping hybrid benchmark")
        return

    # Remove engineered constraints — test learned only
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)

    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp_opts, "WARM_START_FILE", "warm_constraints.csv")

    _, completed_seeds = load_csv_log(benchmark_path)
    bench_seeds = list(range(1, BENCHMARK_SEEDS + 1))

    print(f"[{label}] Hybrid benchmark: {len(bench_seeds)} seeds "
          f"({len(completed_seeds)} done)")

    deadlock_count = 0
    existing_rows, _ = load_csv_log(benchmark_path)
    deadlock_count = sum(1 for r in existing_rows if int(r.get("deadlocked", 0)))

    for i, s in enumerate(bench_seeds):
        if s in completed_seeds:
            continue
        set_csv_option(runctrl, "seed", s)
        try:
            run_exe(workdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] HYBRID ERROR seed {s}: {e}")
            continue

        log_path = os.path.join(output_dir, "dlexp_log.csv")
        results = parse_dlexp_log(log_path)
        if not results:
            continue

        r = results[0]
        if r["deadlock_found"]:
            deadlock_count += 1

        append_csv_row(benchmark_path,
            ["seed", "throughput", "cpu_time", "deadlocked",
             "avg_wait_time", "avg_spawn_delay"],
            [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
             r["deadlock_found"],
             f"{r['avg_wait_time']:.6f}", f"{r['avg_spawn_delay']:.6f}"])

        if (i + 1) % 10 == 0 or i == len(bench_seeds) - 1:
            print(f"[{label}] Hybrid bench {i+1}/{len(bench_seeds)}: "
                  f"{deadlock_count} DL")

    print(f"[{label}] Hybrid benchmark: {deadlock_count}/{len(bench_seeds)} deadlocks")


# === Phase 4: Engineered Benchmark (PL + engineered spawn-hold) ===

def run_engineered_benchmark(N, fraction, workdir, config_dir):
    label = scenario_label(N, fraction)
    rdir = get_results_dir(N, fraction)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(rdir, "engineered_benchmark.csv")

    # Generate and install engineered constraints
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    generate_engineered_constraints(N, nogood)

    # Belt-and-braces: ensure no learned constraints leak into this phase
    warm_file = os.path.join(workdir, "warm_constraints.csv")
    if os.path.exists(warm_file):
        os.rename(warm_file, warm_file + ".bak")

    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")

    _, completed_seeds = load_csv_log(benchmark_path)
    bench_seeds = list(range(1, BENCHMARK_SEEDS + 1))

    print(f"[{label}] Engineered benchmark: {len(bench_seeds)} seeds "
          f"({len(completed_seeds)} done)")

    deadlock_count = 0
    existing_rows, _ = load_csv_log(benchmark_path)
    deadlock_count = sum(1 for r in existing_rows if int(r.get("deadlocked", 0)))

    for i, s in enumerate(bench_seeds):
        if s in completed_seeds:
            continue
        set_csv_option(runctrl, "seed", s)
        try:
            run_exe(workdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] ENG ERROR seed {s}: {e}")
            continue

        log_path = os.path.join(output_dir, "dlexp_log.csv")
        results = parse_dlexp_log(log_path)
        if not results:
            continue

        r = results[0]
        if r["deadlock_found"]:
            deadlock_count += 1

        append_csv_row(benchmark_path,
            ["seed", "throughput", "cpu_time", "deadlocked",
             "avg_wait_time", "avg_spawn_delay"],
            [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
             r["deadlock_found"],
             f"{r['avg_wait_time']:.6f}", f"{r['avg_spawn_delay']:.6f}"])

        if (i + 1) % 10 == 0 or i == len(bench_seeds) - 1:
            print(f"[{label}] Eng bench {i+1}/{len(bench_seeds)}: "
                  f"{deadlock_count} DL")

    # Clean up: remove engineered constraints, restore warm file
    if os.path.exists(nogood):
        os.remove(nogood)
    if os.path.exists(warm_file + ".bak"):
        os.rename(warm_file + ".bak", warm_file)

    print(f"[{label}] Engineered benchmark: {deadlock_count}/{len(bench_seeds)} deadlocks")


# === Scenario runner ===

def run_scenario(N, fraction, skip_training=False):
    label = scenario_label(N, fraction)
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"[{label}] Starting scenario (rate={RATE})")
    print(f"{'='*60}")

    config_dir = generate_base_configs(N)
    workdir = setup_workdir(N, fraction, config_dir)

    # Phase 1: PL benchmark
    pl_deadlocks = run_pl_benchmark(N, fraction, workdir)

    if not skip_training:
        # Phase 2: Training (always run — even if PL has 0 deadlocks, good to confirm)
        if pl_deadlocks > 0:
            run_training(N, fraction, workdir)
            # Phase 3: Hybrid benchmark
            run_hybrid_benchmark(N, fraction, workdir)
        else:
            print(f"[{label}] PL had 0 deadlocks, skipping training")

    # Phase 4: Engineered benchmark (always — confirms engineered constraints work)
    run_engineered_benchmark(N, fraction, workdir, config_dir)

    elapsed = time.time() - t0
    print(f"[{label}] Complete in {elapsed:.0f}s")
    return N, fraction, elapsed


def run_scenario_wrapper(args):
    n, frac, skip_t = args
    try:
        return run_scenario(n, frac, skip_training=skip_t)
    except Exception as e:
        label = scenario_label(n, frac)
        print(f"[{label}] FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        return n, frac, -1


# === Main ===

def main():
    global RATE

    parser = argparse.ArgumentParser(
        description="Overlength train experiment on synthetic corridors")
    parser.add_argument("--num-loops", nargs="+", type=int, default=None,
                        help=f"Corridor sizes (default: {NUM_LOOPS})")
    parser.add_argument("--fractions", nargs="+", type=float, default=None,
                        help=f"Overlength fractions (default: {FRACTIONS})")
    parser.add_argument("--rate", type=float, default=RATE,
                        help=f"Spawn rate (default: {RATE})")
    parser.add_argument("--jobs", "-j", type=int, default=1,
                        help="Parallel workers (default: 1)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-training", action="store_true")
    args = parser.parse_args()

    RATE = args.rate
    num_loops = args.num_loops or NUM_LOOPS
    fractions = args.fractions or FRACTIONS

    if not os.path.exists(EXE_PATH):
        print(f"ERROR: DesRail.exe not found at {EXE_PATH}")
        sys.exit(1)

    scenarios = [(n, f) for n in num_loops for f in fractions]

    print(f"Overlength experiment: {len(scenarios)} scenarios, "
          f"{args.jobs} workers")
    print(f"  N: {num_loops}")
    print(f"  Fractions: {fractions}")
    print(f"  Rate: {RATE}")
    print(f"  Termination: {CLEAN_THRESHOLD} clean seeds, "
          f"OR seeds>{MAX_SEEDS}, OR constraints>{MAX_CONSTRAINTS}")

    if args.dry_run:
        for n, f in scenarios:
            print(f"  {scenario_label(n, f)}")
        return

    # Generate configs for all N values
    print("\nGenerating corridor configs...")
    for n in num_loops:
        generate_base_configs(n)

    t0 = time.time()
    if args.jobs <= 1:
        for n, f in scenarios:
            run_scenario_wrapper((n, f, args.skip_training))
    else:
        worker_args = [(n, f, args.skip_training) for n, f in scenarios]
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(run_scenario_wrapper, a): a
                       for a in worker_args}
            for fut in as_completed(futures):
                n, f, elapsed = fut.result()
                label = scenario_label(n, f)
                if elapsed < 0:
                    print(f"\n*** [{label}] FAILED ***")
                else:
                    print(f"\n*** [{label}] DONE in {elapsed:.0f}s ***")

    total = time.time() - t0
    print(f"\nAll scenarios complete in {total:.0f}s ({total/3600:.1f}h)")


if __name__ == "__main__":
    main()
