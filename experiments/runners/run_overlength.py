#!/usr/bin/env python3
"""
Overlength experiment driver for the 2026-05-20 pruning rerun.

Replicates the paper's overlength campaign: 5 corridor sizes
(N=2,5,10,15,20) x 3 overlength fractions (phi=0.10, 0.25, 0.50) at fixed
lambda=2.00. Four phases per scenario:
  1. PL benchmark        — PL only (engineered nogood_constr.csv removed).
                           Should produce deadlocks.
  2. Training            — PL only, learner discovers replacement constraints.
                           Termination: 100 clean seeds, OR seeds > 3000, OR
                           total_constraints > 25000.
  3. Hybrid benchmark    — PL + learned constraints (engineered still absent).
                           Tests whether learning suffices.
  4. Engineered bench    — PL + engineered spawn-hold constraints (learned
                           constraints temporarily masked). Should be DL-free.

Folder structure (per scenario):
    overlength/N{XX}/f{X.XX}/
        input/                  scenario config (engineered may be absent)
        output/                 last seed's exe output
        pl_benchmark.csv        phase 1 results
        training_log.csv        phase 2 per-seed log
        constraints.csv         final learned constraints
        warm_constraints.csv    working warm-start file
        hybrid_benchmark.csv    phase 3 results (PL + learned)
        engineered_benchmark.csv phase 4 results (PL + engineered)

Usage:
    python run_overlength.py                                    # all 15 scenarios
    python run_overlength.py --scenarios N10:f0.25 N15:f0.50
    python run_overlength.py --setup-only
    python run_overlength.py --skip-training
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))          # .../runners
CAMPAIGN_DIR = os.path.dirname(SCRIPT_DIR)                       # the set folder
SET_LABEL = "overlength"
# Heavy artifacts live under work/ (VM-only); run_all.py harvests only the lean
# analysis CSVs into results/. WORK_DIR is the base for every scenario dir.
WORK_DIR = os.path.join(CAMPAIGN_DIR, "work", SET_LABEL)


def _find_repo_root(start):
    """Find the DesRail repo root (dir containing DesRail/ and x64/)."""
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
    return os.path.dirname(os.path.dirname(CAMPAIGN_DIR))


REPO_ROOT = _find_repo_root(CAMPAIGN_DIR)

sys.path.insert(0, CAMPAIGN_DIR)
from run_overlength_exp import (  # noqa: E402
    set_csv_option,
    parse_dlexp_log,
    load_csv_log,
    run_exe,
    append_csv_row,
    read_base_spawners,
    write_overlength_spawners,
    generate_base_configs as gen_base_for_N,
)

# === Campaign parameters ===

N_VALUES = [2, 5, 10, 15, 20]
SPAWN_RATE = 2.00
FRACTIONS = [0.10, 0.25, 0.50]
CLEAN_THRESHOLD = 100
MAX_SEEDS = 3000
MAX_CONSTRAINTS = 25000
MAX_ITERATIONS_TRAINING = 2000
BENCHMARK_SEEDS = 100
MIN_LENGTH = 0.8  # overlength threshold for engineered constraints

EXE_PATH = os.path.join(REPO_ROOT, "x64", "Release", "DesRail.exe")
GENERATOR_SCRIPT = os.path.join(REPO_ROOT, "DesRail", "generate_corridor.py")
CONSTRAINT_GEN_SCRIPT = os.path.join(REPO_ROOT, "DesRail",
                                     "generate_overlength_constraints.py")


def scenario_label(N, fraction):
    return f"N{N:02d}/f{fraction:.2f}"


def scenario_dir(N, fraction):
    return os.path.join(WORK_DIR, f"N{N:02d}", f"f{fraction:.2f}")


def base_config_dir(N):
    return os.path.join(CAMPAIGN_DIR, "configs_overlength", f"N{N:02d}")


def ensure_base_config(N):
    """Make sure configs_overlength/N{XX} exists. Regenerate if not."""
    cdir = base_config_dir(N)
    marker = os.path.join(cdir, "input", "network.csv")
    if os.path.exists(marker):
        return
    subprocess.run(
        [sys.executable, GENERATOR_SCRIPT,
         "--num_loops", str(N),
         "--output_dir", cdir,
         "--corridor_len", "2.0",
         "--corridor_len_var", "0.5",
         "--loop_len", "0.7",
         "--spawn_rate", "1.0",
         "--horizon", "192",
         "--warmup", "24",
         "--seed", "1",
         "--overlength"],
        check=True,
    )


def generate_engineered_constraints(N, output_path):
    """Generate spawn-hold overlength constraints for N-loop corridor."""
    subprocess.run(
        [sys.executable, CONSTRAINT_GEN_SCRIPT,
         "-n", str(N), "-o", output_path,
         "--min_length", str(MIN_LENGTH)],
        check=True, capture_output=True,
    )


def setup_scenario(N, fraction):
    """Create scenario subdir and populate input/ with the base config.

    Note: engineered nogood_constr.csv is NOT installed here. It's installed
    only for Phase 4 (engineered benchmark) and removed elsewhere.
    """
    ensure_base_config(N)
    sdir = scenario_dir(N, fraction)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    base_input = os.path.join(base_config_dir(N), "input")
    for fname in os.listdir(base_input):
        src = os.path.join(base_input, fname)
        dst = os.path.join(input_dir, fname)
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)

    # Be defensive: a stale nogood_constr.csv from a prior run would pollute
    # the PL-only phases. Remove it on setup so we always start clean.
    stale_nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(stale_nogood):
        os.remove(stale_nogood)

    # Mixed-fleet spawners
    base_spawners = read_base_spawners(base_config_dir(N))
    write_overlength_spawners(
        os.path.join(input_dir, "open_loop_spawners.csv"),
        SPAWN_RATE, fraction, base_spawners)

    # Configure dlexp options (CONSTRAINT_EVAL=flat per tree-eval bug memo)
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")

    return sdir


# === Phase 1: PL Benchmark (no engineered) ===

def run_pl_benchmark(N, fraction):
    label = scenario_label(N, fraction)
    sdir = scenario_dir(N, fraction)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(sdir, "pl_benchmark.csv")

    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")

    # PL-only: engineered constraints must not be loaded.
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)

    _, done = load_csv_log(benchmark_path)
    existing, _ = load_csv_log(benchmark_path)
    deadlocks = sum(1 for r in existing if int(r.get("deadlocked", 0)))

    for s in range(1, BENCHMARK_SEEDS + 1):
        if s in done:
            continue
        set_csv_option(runctrl, "seed", s)
        try:
            run_exe(sdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] PL bench ERROR seed {s}: {e}")
            continue
        results = parse_dlexp_log(os.path.join(output_dir, "dlexp_log.csv"))
        if not results:
            continue
        r = results[0]
        if r["deadlock_found"]:
            deadlocks += 1
        append_csv_row(benchmark_path,
            ["seed", "throughput", "cpu_time", "deadlocked",
             "avg_wait_time", "avg_spawn_delay"],
            [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
             r["deadlock_found"],
             f"{r['avg_wait_time']:.6f}", f"{r['avg_spawn_delay']:.6f}"])
    print(f"[{label}] PL bench done: {deadlocks} deadlocks/{BENCHMARK_SEEDS}")
    return deadlocks


# === Phase 2: Training (PL only, no engineered) ===

def run_training(N, fraction):
    label = scenario_label(N, fraction)
    sdir = scenario_dir(N, fraction)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(sdir, "training_log.csv")
    constraints_result = os.path.join(sdir, "constraints.csv")
    warm_file = os.path.join(sdir, "warm_constraints.csv")

    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(runctrl, "pl_constraints", 1)

    # PL-only: learner must discover the engineered constraints itself.
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)

    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)

    existing, done = load_csv_log(training_log)
    consecutive_clean = 0
    for row in reversed(existing):
        if int(row["new_constraints"]) == 0:
            consecutive_clean += 1
        else:
            break
    seed = (max(done) + 1) if done else 1
    total_constraints = (int(existing[-1]["total_constraints"]) if existing else 0)
    training_seeds_count = sum(1 for r in existing if int(r["new_constraints"]) > 0)

    if consecutive_clean >= CLEAN_THRESHOLD:
        print(f"[{label}] Training already converged: {total_constraints}c")
        return total_constraints

    bail_reason = None
    while consecutive_clean < CLEAN_THRESHOLD and seed <= MAX_SEEDS:
        if total_constraints > MAX_CONSTRAINTS:
            bail_reason = "max_constraints"
            break
        set_csv_option(runctrl, "seed", seed)
        set_csv_option(dlexp_opts, "WARM_START_FILE",
                       "warm_constraints.csv" if os.path.exists(warm_file) else "")
        try:
            run_exe(sdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] Train ERROR seed {seed}: {e}")
            bail_reason = "exe_error"
            break
        log_path = os.path.join(output_dir, "dlexp_log.csv")
        if not os.path.exists(log_path):
            bail_reason = "no_log"
            break
        results = parse_dlexp_log(log_path)
        if not results:
            bail_reason = "empty_log"
            break

        new_c = sum(r["new_constraints"] for r in results)
        total_constraints = results[-1]["total_constraints"]
        iterations = len(results)
        trains_completed = results[-1]["trains_completed"]
        trains_per_hr = results[-1]["trains_per_hr"]
        cpu = sum(r["sim_run_time"] for r in results)
        deadlock_found = 1 if new_c > 0 else 0

        if new_c > 0:
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
            [seed, deadlock_found, new_c, total_constraints,
             iterations, trains_completed, f"{trains_per_hr:.4f}", f"{cpu:.3f}"])
        status = "CLEAN" if new_c == 0 else f"+{new_c}c"
        print(f"[{label}] seed {seed}: {status} "
              f"(total={total_constraints}c, {consecutive_clean}/{CLEAN_THRESHOLD} clean)")
        seed += 1

    if bail_reason is None:
        bail_reason = "converged" if consecutive_clean >= CLEAN_THRESHOLD else "max_seeds"
    print(f"[{label}] Training done ({bail_reason}): {total_constraints}c, "
          f"{training_seeds_count} training seeds, {seed - 1} total seeds")
    return total_constraints


# === Phase 3: Hybrid Benchmark (PL + learned, no engineered) ===

def run_hybrid_benchmark(N, fraction):
    label = scenario_label(N, fraction)
    sdir = scenario_dir(N, fraction)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(sdir, "hybrid_benchmark.csv")
    warm_file = os.path.join(sdir, "warm_constraints.csv")
    constraints_result = os.path.join(sdir, "constraints.csv")

    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)
    if not os.path.exists(warm_file):
        print(f"[{label}] No learned constraints, skipping hybrid benchmark")
        return

    # Engineered must be absent (test learned only).
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)

    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp_opts, "WARM_START_FILE", "warm_constraints.csv")

    _, done = load_csv_log(benchmark_path)
    existing, _ = load_csv_log(benchmark_path)
    deadlocks = sum(1 for r in existing if int(r.get("deadlocked", 0)))

    for s in range(1, BENCHMARK_SEEDS + 1):
        if s in done:
            continue
        set_csv_option(runctrl, "seed", s)
        try:
            run_exe(sdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] Hybrid bench ERROR seed {s}: {e}")
            continue
        results = parse_dlexp_log(os.path.join(output_dir, "dlexp_log.csv"))
        if not results:
            continue
        r = results[0]
        if r["deadlock_found"]:
            deadlocks += 1
        append_csv_row(benchmark_path,
            ["seed", "throughput", "cpu_time", "deadlocked",
             "avg_wait_time", "avg_spawn_delay"],
            [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
             r["deadlock_found"],
             f"{r['avg_wait_time']:.6f}", f"{r['avg_spawn_delay']:.6f}"])
    print(f"[{label}] Hybrid bench done: {deadlocks} deadlocks/{BENCHMARK_SEEDS}")


# === Phase 4: Engineered Benchmark (PL + engineered, no learned) ===

def run_engineered_benchmark(N, fraction):
    label = scenario_label(N, fraction)
    sdir = scenario_dir(N, fraction)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(sdir, "engineered_benchmark.csv")
    warm_file = os.path.join(sdir, "warm_constraints.csv")

    # Install engineered constraints.
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    generate_engineered_constraints(N, nogood)

    # Mask learned constraints so we're testing engineered only.
    warm_bak = warm_file + ".bak"
    if os.path.exists(warm_file):
        os.rename(warm_file, warm_bak)

    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")

    _, done = load_csv_log(benchmark_path)
    existing, _ = load_csv_log(benchmark_path)
    deadlocks = sum(1 for r in existing if int(r.get("deadlocked", 0)))

    try:
        for s in range(1, BENCHMARK_SEEDS + 1):
            if s in done:
                continue
            set_csv_option(runctrl, "seed", s)
            try:
                run_exe(sdir)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                print(f"[{label}] Eng bench ERROR seed {s}: {e}")
                continue
            results = parse_dlexp_log(os.path.join(output_dir, "dlexp_log.csv"))
            if not results:
                continue
            r = results[0]
            if r["deadlock_found"]:
                deadlocks += 1
            append_csv_row(benchmark_path,
                ["seed", "throughput", "cpu_time", "deadlocked",
                 "avg_wait_time", "avg_spawn_delay"],
                [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
                 r["deadlock_found"],
                 f"{r['avg_wait_time']:.6f}", f"{r['avg_spawn_delay']:.6f}"])
    finally:
        if os.path.exists(nogood):
            os.remove(nogood)
        if os.path.exists(warm_bak):
            os.rename(warm_bak, warm_file)

    print(f"[{label}] Engineered bench done: {deadlocks} deadlocks/{BENCHMARK_SEEDS}")


# === Per-scenario driver ===

def run_scenario(N, fraction, skip_training=False):
    label = scenario_label(N, fraction)
    t0 = time.time()
    setup_scenario(N, fraction)

    pl_dl = run_pl_benchmark(N, fraction)

    if not skip_training:
        if pl_dl > 0:
            run_training(N, fraction)
            run_hybrid_benchmark(N, fraction)
        else:
            print(f"[{label}] PL had 0 deadlocks, skipping training + hybrid")

    # Phase 4 always runs — sanity check that engineered constraints work.
    run_engineered_benchmark(N, fraction)

    elapsed = time.time() - t0
    print(f"[{label}] Scenario complete in {elapsed:.0f}s")


def wrapper(args):
    N, fraction, skip_training = args
    try:
        run_scenario(N, fraction, skip_training=skip_training)
    except Exception as e:
        print(f"[{scenario_label(N, fraction)}] CRASH: {e}")


# === CLI ===

def parse_scenarios(specs):
    if not specs:
        return [(N, f) for N in N_VALUES for f in FRACTIONS]
    out = []
    for spec in specs:
        n_part, f_part = spec.split(":")
        N = int(n_part.lstrip("N"))
        frac = float(f_part.lstrip("f"))
        out.append((N, frac))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenarios", nargs="+",
                   help="N{XX}:f{X.XX} (default: 15 = 5 N x 3 fractions)")
    p.add_argument("--jobs", type=int, default=12)
    p.add_argument("--setup-only", action="store_true")
    p.add_argument("--skip-training", action="store_true")
    args = p.parse_args()

    scenarios = parse_scenarios(args.scenarios)
    print(f"Overlength rerun: {len(scenarios)} scenarios | jobs={args.jobs} | "
          f"lambda={SPAWN_RATE} | MAX_SEEDS={MAX_SEEDS} | MAX_CONSTRAINTS={MAX_CONSTRAINTS}")

    print("Setting up scenario directories...")
    for N, fraction in scenarios:
        setup_scenario(N, fraction)
    print(f"Setup complete: {len(scenarios)} scenarios under {WORK_DIR}")

    if args.setup_only:
        return 0

    work = [(N, frac, args.skip_training) for N, frac in scenarios]
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            list(ex.map(wrapper, work))
    else:
        for w in work:
            wrapper(w)

    print("Overlength campaign complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
