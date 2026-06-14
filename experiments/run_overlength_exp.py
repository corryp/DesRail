#!/usr/bin/env python3
"""
Experiment: overlength trains on PL-configured corridors.

Tests what happens when a fraction of trains exceed passing loop siding length.
PL constraints assume trains fit in sidings; overlength trains break this assumption.

Phases per scenario:
  1. PL benchmark  — 100 seeds with PL constraints, count deadlocks
  2. Training      — if PL deadlocked, run automated constraint learning
  3. Learned bench — 100 seeds with learned constraints

Usage:
    python run_overlength_exp.py                          # All scenarios (default N=10)
    python run_overlength_exp.py -n 20 --rates 3.00      # N=20 at rate 3.0
    python run_overlength_exp.py --jobs 4                 # 4 scenarios in parallel
    python run_overlength_exp.py --scenarios r2.00:f0.25  # Single scenario
    python run_overlength_exp.py --skip-training          # PL benchmark only
    python run_overlength_exp.py --dry-run
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
GENERATOR_SCRIPT = os.path.join(REPO_ROOT, "DesRail", "generate_corridor.py")
CONSTRAINT_GEN_SCRIPT = os.path.join(REPO_ROOT, "DesRail", "generate_overlength_constraints.py")

# === Experiment parameters (defaults, overridden by CLI) ===

N = 10
RATES = [2.00, 2.50]
FRACTIONS = [0.0, 0.10, 0.25, 0.50, 0.75, 1.00]

WARMUP = 24
HORIZON = 168 + WARMUP
CORRIDOR_LEN = 2.0
CORRIDOR_LEN_VAR = 0.5
LOOP_LEN = 0.7
CLEAN_THRESHOLD = 100
MAX_ITERATIONS_TRAINING = 2000
MAX_SEEDS = 5000
EXE_TIMEOUT = 28800
BENCHMARK_SEEDS = 100


# === Helpers (same as run_experiments.py) ===

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
    return result


def append_csv_row(filepath, header, row):
    """Append a row to a CSV file, writing header if file doesn't exist."""
    write_header = not os.path.exists(filepath)
    with open(filepath, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerow(row)


# === Labels and paths ===

def scenario_label(rate, fraction):
    return f"r{rate:.2f}/f{fraction:.2f}"


def get_results_dir(rate, fraction):
    return os.path.join(SCRIPT_DIR, "results_overlength",
                        f"N{N:02d}", f"r{rate:.2f}", f"f{fraction:.2f}")


# === Config generation ===

def generate_base_configs():
    """Generate base N=10 corridor configs (with freight_long template)."""
    config_dir = os.path.join(SCRIPT_DIR, "configs_overlength", f"N{N:02d}")
    marker = os.path.join(config_dir, "input", "network.csv")
    if os.path.exists(marker):
        print(f"  N={N}: configs exist, skipping")
        return

    subprocess.run(
        [sys.executable, GENERATOR_SCRIPT,
         "--num_loops", str(N),
         "--output_dir", config_dir,
         "--corridor_len", str(CORRIDOR_LEN),
         "--corridor_len_var", str(CORRIDOR_LEN_VAR),
         "--loop_len", str(LOOP_LEN),
         "--spawn_rate", "1.0",  # placeholder
         "--horizon", str(HORIZON),
         "--warmup", str(WARMUP),
         "--seed", "1",
         "--overlength"],
        check=True,
        capture_output=True,
    )
    print(f"  N={N}: generated")


# === Spawner setup ===

def read_base_spawners(config_dir):
    """Read base spawner rows. Each row is one entry point dedicated to its train_template."""
    spawner_path = os.path.join(config_dir, "input", "open_loop_spawners.csv")
    spawners = []
    with open(spawner_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            spawners.append({
                "name": row["name"],
                "spawn_seg": row["spawn_seg"],
                "spawn_head": row["spawn_head"],
                "train_template": row["train_template"],
                "terminal1": row["terminal1"],
                "dwell1": row["dwell1"],
                "terminal2": row["terminal2"],
                "dwell2": row["dwell2"],
            })
    return spawners


def write_overlength_spawners(filepath, rate, fraction, base_spawners):
    """Write open_loop_spawners.csv, scaling each base spawner's rate by its template.

    Base config has one normal-entry ('freight') and one long-entry ('freight_long')
    spawner per direction. Cumulative rate per direction = `rate`:
      freight       -> rate * (1 - fraction)
      freight_long  -> rate * fraction
    """
    header = ["name", "spawn_seg", "spawn_head", "train_template", "distribution",
              "dist_prm1", "dist_prm2", "terminal1", "dwell1", "terminal2", "dwell2"]

    rows = []
    for bs in base_spawners:
        tmpl = bs["train_template"]
        if tmpl == "freight":
            scaled = rate * (1.0 - fraction)
        elif tmpl == "freight_long":
            scaled = rate * fraction
        else:
            raise ValueError(
                f"Unknown train_template {tmpl!r} in base spawner {bs['name']!r}")
        if scaled <= 0.0:
            continue
        rows.append([
            bs["name"], bs["spawn_seg"], bs["spawn_head"],
            tmpl, "exponential", scaled, 0,
            bs["terminal1"], bs["dwell1"], bs["terminal2"], bs["dwell2"],
        ])

    with open(filepath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow(row)


# === Working directory ===

def setup_workdir(rate, fraction, base_spawners):
    """Create/update working directory for a scenario."""
    workdir = os.path.join(SCRIPT_DIR, "work_overlength",
                           f"N{N:02d}", f"r{rate:.2f}", f"f{fraction:.2f}")
    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # Copy base configs
    base_input = os.path.join(SCRIPT_DIR, "configs_overlength",
                              f"N{N:02d}", "input")
    for fname in os.listdir(base_input):
        src = os.path.join(base_input, fname)
        dst = os.path.join(input_dir, fname)
        if os.path.isfile(src):
            shutil.copy2(src, dst)

    # Generate engineered overlength constraints for this corridor size
    nogood_dst = os.path.join(input_dir, "nogood_constr.csv")
    subprocess.run(
        [sys.executable, CONSTRAINT_GEN_SCRIPT,
         "-n", str(N), "-o", nogood_dst],
        check=True, capture_output=True,
    )

    # Write scenario-specific spawners
    write_overlength_spawners(
        os.path.join(input_dir, "open_loop_spawners.csv"),
        rate, fraction, base_spawners)

    # Remove stale warm_constraints.csv from previous runs
    warm_file = os.path.join(workdir, "warm_constraints.csv")
    if os.path.exists(warm_file):
        os.remove(warm_file)

    return workdir


# === Phase 1: PL Benchmark ===

def run_pl_benchmark(rate, fraction, workdir):
    """Run 100 seeds with PL constraints, count deadlocks."""
    label = scenario_label(rate, fraction)
    rdir = get_results_dir(rate, fraction)
    os.makedirs(rdir, exist_ok=True)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(rdir, "pl_benchmark.csv")

    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")

    _, completed_seeds = load_csv_log(benchmark_path)
    bench_seeds = list(range(1, BENCHMARK_SEEDS + 1))

    print(f"[{label}] PL benchmark: {len(bench_seeds)} seeds "
          f"({len(completed_seeds)} already done)")

    existing_rows, _ = load_csv_log(benchmark_path)
    deadlock_count = sum(1 for r in existing_rows if int(r.get("deadlocked", 0)))

    for i, s in enumerate(bench_seeds):
        if s in completed_seeds:
            continue

        set_csv_option(runctrl, "seed", s)

        try:
            run_exe(workdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] PL BENCHMARK ERROR seed {s}: {e}")
            continue

        log_path = os.path.join(output_dir, "dlexp_log.csv")
        results = parse_dlexp_log(log_path)
        if not results:
            print(f"[{label}] PL BENCHMARK ERROR: empty results for seed {s}")
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
            print(f"[{label}] PL benchmark {i + 1}/{len(bench_seeds)}: "
                  f"{deadlock_count} deadlocks so far")

    print(f"[{label}] PL benchmark complete: {deadlock_count} deadlocks "
          f"in {len(bench_seeds)} seeds")
    return deadlock_count


# === Phase 2: Training ===

def run_training(rate, fraction, workdir):
    """Run iterative deadlock avoidance training."""
    label = scenario_label(rate, fraction)
    rdir = get_results_dir(rate, fraction)
    os.makedirs(rdir, exist_ok=True)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(rdir, "training_log.csv")
    constraints_result = os.path.join(rdir, "constraints.csv")
    warm_file = os.path.join(workdir, "warm_constraints.csv")

    # Configure for training — keep PL constraints ON so automated learning
    # only discovers additional constraints needed for overlength trains
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(runctrl, "pl_constraints", 1)

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

    if consecutive_clean >= CLEAN_THRESHOLD:
        total_c = int(existing_rows[-1]["total_constraints"]) if existing_rows else 0
        print(f"[{label}] Training already complete "
              f"({total_c}c, {consecutive_clean} clean seeds)")
        return total_c

    total_constraints = 0
    training_seeds_count = sum(
        1 for r in existing_rows if int(r["new_constraints"]) > 0)

    while consecutive_clean < CLEAN_THRESHOLD and seed <= MAX_SEEDS:
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

    print(f"[{label}] Training done: {total_constraints}c from "
          f"{training_seeds_count} training seeds, {seed - 1} total seeds")
    return total_constraints


# === Phase 3: Learned Benchmark ===

def run_learned_benchmark(rate, fraction, workdir):
    """Run 100 seeds with learned constraints."""
    label = scenario_label(rate, fraction)
    rdir = get_results_dir(rate, fraction)

    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(rdir, "learned_benchmark.csv")
    warm_file = os.path.join(workdir, "warm_constraints.csv")
    constraints_result = os.path.join(rdir, "constraints.csv")

    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)

    if not os.path.exists(warm_file):
        print(f"[{label}] No learned constraints, skipping learned benchmark")
        return

    # PL + learned constraints together (hybrid)
    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp_opts, "WARM_START_FILE", "warm_constraints.csv")

    _, completed_seeds = load_csv_log(benchmark_path)
    bench_seeds = list(range(1, BENCHMARK_SEEDS + 1))

    print(f"[{label}] Learned benchmark: {len(bench_seeds)} seeds "
          f"({len(completed_seeds)} already done)")

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
            print(f"[{label}] LEARNED BENCHMARK ERROR seed {s}: {e}")
            continue

        log_path = os.path.join(output_dir, "dlexp_log.csv")
        results = parse_dlexp_log(log_path)
        if not results:
            print(f"[{label}] LEARNED BENCHMARK ERROR: empty results seed {s}")
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
            print(f"[{label}] Learned benchmark {i + 1}/{len(bench_seeds)}: "
                  f"{deadlock_count} deadlocks so far")

    print(f"[{label}] Learned benchmark complete: {deadlock_count} deadlocks")
    return deadlock_count


# === Scenario runner ===

def run_scenario(rate, fraction, base_spawners, skip_training=False):
    """Run full experiment for one (rate, fraction) scenario."""
    label = scenario_label(rate, fraction)
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"[{label}] Starting scenario")
    print(f"{'='*60}")

    workdir = setup_workdir(rate, fraction, base_spawners)

    # Phase 1: PL benchmark
    pl_deadlocks = run_pl_benchmark(rate, fraction, workdir)

    if skip_training:
        elapsed = time.time() - t0
        print(f"[{label}] Scenario complete in {elapsed:.0f}s (PL only)")
        return rate, fraction, elapsed

    # Phase 2: Training (only if PL had deadlocks)
    if pl_deadlocks > 0:
        print(f"[{label}] PL had {pl_deadlocks} deadlocks, starting training...")
        run_training(rate, fraction, workdir)

        # Phase 3: Learned benchmark
        run_learned_benchmark(rate, fraction, workdir)
    else:
        print(f"[{label}] PL had 0 deadlocks, skipping training")

    elapsed = time.time() - t0
    print(f"[{label}] Scenario complete in {elapsed:.0f}s")
    return rate, fraction, elapsed


def run_scenario_wrapper(args):
    """Wrapper for ProcessPoolExecutor (unpacks tuple)."""
    rate, fraction, skip_training, num_loops = args
    global N
    N = num_loops
    # Re-read base spawners in each worker process
    base_config = os.path.join(SCRIPT_DIR, "configs_overlength", f"N{N:02d}")
    base_spawners = read_base_spawners(base_config)
    try:
        return run_scenario(rate, fraction, base_spawners,
                            skip_training=skip_training)
    except Exception as e:
        label = scenario_label(rate, fraction)
        print(f"[{label}] FATAL ERROR: {e}")
        return rate, fraction, -1


# === Debug mode ===

def run_debug_seed(rate, fraction, seed, base_spawners):
    """Run a single seed with DOT export for manual inspection."""
    label = scenario_label(rate, fraction)
    print(f"\n{'='*60}")
    print(f"[{label}] Debug seed {seed}")
    print(f"{'='*60}")

    workdir = setup_workdir(rate, fraction, base_spawners)
    input_dir = os.path.join(workdir, "input")
    output_dir = os.path.join(workdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")

    # Enable debug output: DOT files + verbose logging from iteration 0
    set_csv_option(dlexp_opts, "START_DEBUG", 0)
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 2000)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")
    set_csv_option(runctrl, "seed", seed)
    set_csv_option(runctrl, "pl_constraints", 1)

    print(f"  Workdir: {workdir}")
    print(f"  PL constraints: ON")
    print(f"  MAX_ITERATIONS: 10")
    print(f"  START_DEBUG: 0 (DOT export enabled)")

    try:
        run_exe(workdir)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"  ERROR: {e}")
        return

    # Print dlexp_log summary
    log_path = os.path.join(output_dir, "dlexp_log.csv")
    if os.path.exists(log_path):
        results = parse_dlexp_log(log_path)
        print(f"\n  dlexp_log.csv: {len(results)} iterations")
        for r in results:
            dl = "DEADLOCK" if r["deadlock_found"] else "clean"
            print(f"    iter {r['iteration']}: {dl} "
                  f"t={r['deadlock_time']:.1f} "
                  f"+{r['new_constraints']}c "
                  f"(total={r['total_constraints']})")
    else:
        print("  No dlexp_log.csv produced")

    # List DOT files
    dot_files = [f for f in os.listdir(output_dir) if f.endswith(".dot")]
    if dot_files:
        print(f"\n  DOT files ({len(dot_files)}):")
        for f in sorted(dot_files):
            print(f"    {os.path.join(output_dir, f)}")
    else:
        print("\n  No DOT files produced")

    print(f"\n  Inspect output at: {output_dir}")


# === Main ===

def parse_scenario_spec(spec):
    """Parse 'r2.00:f0.25' into (2.0, 0.25)."""
    parts = spec.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid spec '{spec}', expected 'r#.##:f#.##'")
    r_str, f_str = parts
    if not r_str.startswith("r"):
        raise ValueError(f"Invalid rate spec '{r_str}', expected 'r#.##'")
    if not f_str.startswith("f"):
        raise ValueError(f"Invalid fraction spec '{f_str}', expected 'f#.##'")
    return float(r_str[1:]), float(f_str[1:])


def main():
    global N, RATES

    parser = argparse.ArgumentParser(
        description="Overlength train experiment on synthetic corridors")
    parser.add_argument("-n", "--num-loops", type=int, default=N,
                        help=f"Number of passing loops (default: {N})")
    parser.add_argument("--rates", nargs="+", type=float, default=None,
                        help="Spawn rates to test (default: 2.00 2.50)")
    parser.add_argument("--scenarios", nargs="+", metavar="r#.##:f#.##",
                        help="Run specific scenarios (e.g., r2.00:f0.25)")
    parser.add_argument("--jobs", "-j", type=int, default=1,
                        help="Number of parallel scenario workers (default: 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print scenarios without running")
    parser.add_argument("--skip-training", action="store_true",
                        help="Run PL benchmark only")
    parser.add_argument("--debug-seed", type=int, metavar="SEED",
                        help="Run single seed with DOT export for inspection")
    parser.add_argument("--clean", action="store_true",
                        help="Delete work/results dirs for specified scenarios first")
    args = parser.parse_args()

    N = args.num_loops
    if args.rates is not None:
        RATES = args.rates

    # Verify exe exists
    if not os.path.exists(EXE_PATH):
        print(f"ERROR: DesRail.exe not found at {EXE_PATH}")
        print("Build the C++ simulator first (Release|x64).")
        sys.exit(1)

    # Build scenario list
    if args.scenarios:
        scenarios = [parse_scenario_spec(s) for s in args.scenarios]
    else:
        scenarios = [(rate, frac) for rate in RATES for frac in FRACTIONS]

    print(f"Overlength experiment: {len(scenarios)} scenarios, N={N}, "
          f"{args.jobs} workers")
    print(f"  Overlength train: 2 locos + 38 cars = 0.80 km "
          f"(siding = {LOOP_LEN} km)")

    if args.dry_run:
        for rate, frac in scenarios:
            print(f"  {scenario_label(rate, frac)}")
        return

    # Generate base configs
    print("\nGenerating base configs...")
    generate_base_configs()

    # Read base spawner geometry (spawn_seg, spawn_head, terminals)
    base_config = os.path.join(SCRIPT_DIR, "configs_overlength", f"N{N:02d}")
    base_spawners = read_base_spawners(base_config)

    # Clean work/results directories if requested
    if args.clean:
        for rate, frac in scenarios:
            label = scenario_label(rate, frac)
            workdir = os.path.join(SCRIPT_DIR, "work_overlength",
                                   f"N{N:02d}", f"r{rate:.2f}", f"f{frac:.2f}")
            rdir = get_results_dir(rate, frac)
            for d in [workdir, rdir]:
                if os.path.exists(d):
                    shutil.rmtree(d)
                    print(f"[{label}] Cleaned {d}")

    # Debug mode: single seed with DOT export
    if args.debug_seed is not None:
        if len(scenarios) != 1:
            print("ERROR: --debug-seed requires exactly one --scenarios spec")
            sys.exit(1)
        rate, frac = scenarios[0]
        run_debug_seed(rate, frac, args.debug_seed, base_spawners)
        return

    # Run scenarios
    t0 = time.time()
    if args.jobs <= 1:
        for rate, frac in scenarios:
            try:
                run_scenario(rate, frac, base_spawners,
                             skip_training=args.skip_training)
            except Exception as e:
                label = scenario_label(rate, frac)
                print(f"[{label}] FATAL ERROR: {e}")
    else:
        worker_args = [(rate, frac, args.skip_training, N)
                       for rate, frac in scenarios]
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(run_scenario_wrapper, a): a
                       for a in worker_args}
            for future in as_completed(futures):
                rate, frac, elapsed = future.result()
                if elapsed >= 0:
                    print(f"[{scenario_label(rate, frac)}] DONE ({elapsed:.0f}s)")

    total = time.time() - t0
    print(f"\nAll scenarios complete in {total:.0f}s ({total/3600:.1f}h)")


if __name__ == "__main__":
    main()
