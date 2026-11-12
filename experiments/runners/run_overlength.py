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
# DESRAIL_WORK moves the work tree off the shared filesystem. Aqua home is
# Lustre, which handles many small files poorly, and a run writes a fresh
# input/+output/ plus a ~140 KB per-train log, 97% of its output. Unset,
# this is the bundle-local path, so nothing already configured moves.
# Whether it is worth using is a measurement to take on a compute node,
# not an assumption: the 2.1x figure in the decision queue is drvfs on the
# laptop and does not transfer.
WORK_DIR = os.path.join(
    os.environ.get("DESRAIL_WORK") or os.path.join(CAMPAIGN_DIR, "work"),
    SET_LABEL)


def _find_repo_root(start):
    """Find the DesRail repo root (the dir containing DesRail/)."""
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

sys.path.insert(0, CAMPAIGN_DIR)
from run_overlength_exp import (  # noqa: E402
    set_csv_option,
    parse_dlexp_log,
    load_csv_log,
    run_exe,
    apply_drain,
    bench_outcome,
    append_csv_row,
    read_base_spawners,
    write_overlength_spawners,
    generate_base_configs as gen_base_for_N,
    set_or_add_csv_option,
)

# === Campaign parameters ===

N_VALUES = [2, 5, 10, 15, 20]
SPAWN_RATE = 2.00
FRACTIONS = [0.10, 0.25, 0.50]
CLEAN_THRESHOLD = 100
MAX_SEEDS = 10000
MAX_CONSTRAINTS = 20000
MAX_ITERATIONS_TRAINING = 2000
# A third halting condition beside the seed and constraint bail-outs. Under
# `discard` a cell whose replications nearly all hit the iteration cap keeps
# nothing, so the constraint bail-out never fires and the seed bail-out sits
# 10,000 replications away; four Toowoomba pure cells and three
# overlength-ablation cells are in that regime. 96 hours is above every cell
# measured in the 2026-10-22 campaign (heaviest 20.1 h, corridor/discard/N20/
# r2.75), so it changes nothing already run, and it is cumulative across PBS
# resumes because training_log.csv carries cpu_time per seed.
MAX_CPU_HOURS = 96
# The campaign benchmarked seeds 1 to 100, which training also walks, so those
# benchmarks are in-sample (review comment 1.8). BENCH_SEED_START=100000 runs
# the same three phases on the corridor's held-out range instead, writing
# *_heldout.csv beside the originals, which stay as they were.
# BENCH_SEED_COUNT=10000 extends a held-out file to the corridor's 10,000 seeds;
# seeds already in the file are skipped, so the first 100 are kept.
BENCHMARK_SEEDS = int(os.environ.get("BENCH_SEED_COUNT", "100"))
BENCH_SEED_START = int(os.environ.get("BENCH_SEED_START", "1"))
BENCH_SUFFIX = "" if BENCH_SEED_START == 1 else "_heldout"
BENCH_SEEDS = range(BENCH_SEED_START, BENCH_SEED_START + BENCHMARK_SEEDS)
MIN_LENGTH = 0.8  # overlength threshold for engineered constraints

# DESRAIL_EXE points at the native binary for a Linux or HPC run; unset, this
# is the Windows build the laptop uses, so nothing already configured moves.
EXE_PATH = os.environ.get("DESRAIL_EXE") or os.path.join(
    REPO_ROOT, "x64", "Release", "DesRail.exe")
GENERATOR_SCRIPT = os.path.join(REPO_ROOT, "DesRail", "generate_corridor.py")
CONSTRAINT_GEN_SCRIPT = os.path.join(REPO_ROOT, "DesRail",
                                     "generate_overlength_constraints.py")


def scenario_label(N, fraction):
    return f"N{N:02d}/f{fraction:.2f}"


def stall_policy():
    """What happens to a replication that never resolved within the iteration cap.
    `bank` keeps its constraints, which is the published behaviour; `discard`
    throws them away, keeping only what a replication that drained by itself
    found. Measured in the revision's D6 study: discard reaches a markedly
    smaller set at equal held-out safety. Both arms are run and compared, so the
    policy is part of a cell's identity and appears in its path.

    Read from the environment rather than passed down, so pool workers inherit it
    whatever process start method is used.
    """
    p = os.environ.get("STALL_POLICY", "bank")
    if p not in ("bank", "discard"):
        raise ValueError(f"STALL_POLICY must be 'bank' or 'discard', got {p!r}")
    return p


def scenario_dir(N, fraction):
    return os.path.join(WORK_DIR, stall_policy(), f"N{N:02d}", f"f{fraction:.2f}")


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
    """Generate spawn-hold overlength constraints for N-loop corridor.

    The generator's lock-through forms are omitted. A footprint is head-anchored
    and matched as a prefix of the train's occupancy, so a constraint naming the
    opposing train's mainline arc already fires when that train is locked
    through the loop onto it; the 8N-4 lock-through forms are subsumed and were
    measured inert over 1,500 paired seeds. Set LOCK_THROUGH=1 to emit them, as
    the 2026-10-22 campaign did, which is the only way to reproduce that
    campaign's engineered_benchmark_lockthrough.csv.
    """
    cmd = [sys.executable, CONSTRAINT_GEN_SCRIPT,
           "-n", str(N), "-o", output_path,
           "--min_length", str(MIN_LENGTH)]
    if os.environ.get("LOCK_THROUGH", "") not in ("1", "true", "yes"):
        cmd.append("--no-lock-through")
    subprocess.run(cmd, check=True, capture_output=True)


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

    # Drain (inherited by all phases via this runctrl): late deadlocks are
    # detected during training so they get learned and convergence is fair.
    apply_drain(os.path.join(input_dir, "runctrl.csv"))

    return sdir


# === Phase 1: PL Benchmark (no engineered) ===

def run_pl_benchmark(N, fraction):
    label = scenario_label(N, fraction)
    sdir = scenario_dir(N, fraction)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(sdir, f"pl_benchmark{BENCH_SUFFIX}.csv")

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

    for s in BENCH_SEEDS:
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
        failed, clear = bench_outcome(r)
        if failed:
            deadlocks += 1
        append_csv_row(benchmark_path,
            ["seed", "throughput", "cpu_time", "deadlocked",
             "avg_wait_time", "avg_spawn_delay", "drained_clear"],
            [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
             failed,
             f"{r['avg_wait_time']:.6f}", f"{r['avg_spawn_delay']:.6f}", clear])
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
        # A clean seed is one the run completed without a deadlock or an unresolved
        # stall, which is what deadlock_found records from 2026-10-19 on. Reading
        # new_constraints instead treats a deadlock that generated nothing as clean.
        if int(row["deadlock_found"]) == 0:
            consecutive_clean += 1
        else:
            break
    seed = (max(done) + 1) if done else 1
    total_constraints = (int(existing[-1]["total_constraints"]) if existing else 0)
    training_seeds_count = sum(1 for r in existing if int(r["new_constraints"]) > 0)
    cpu_total = sum(float(r.get("cpu_time", 0)) for r in existing)

    if consecutive_clean >= CLEAN_THRESHOLD:
        print(f"[{label}] Training already converged: {total_constraints}c")
        return total_constraints

    bail_reason = None
    while consecutive_clean < CLEAN_THRESHOLD and seed <= MAX_SEEDS:
        if total_constraints > MAX_CONSTRAINTS:
            bail_reason = "max_constraints"
            break
        if cpu_total > MAX_CPU_HOURS * 3600:
            bail_reason = "max_cpu"
            break
        set_csv_option(runctrl, "seed", seed)
        # Appendix D draws its worked examples from the new campaign, so the SCC
        # behind every generated constraint has to survive the run (decision
        # queue, rerun note 8). Copied out below for seeds that generate.
        set_or_add_csv_option(dlexp_opts, "SCC_LOG", 1)
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
        # Under `discard`, a replication that never resolved contributes nothing:
        # not updating the warm file is what throws its constraints away, since
        # the next seed warm-starts from that file. `resolved` means the run
        # reached a clean replication of its own accord rather than being cut off
        # at the iteration cap.
        resolved = bool(results) and len(results) < MAX_ITERATIONS_TRAINING \
            and not results[-1]["deadlock_found"]
        keep = stall_policy() == "bank" or resolved
        # The exe's cumulative count is relative to the warm set it loaded, so it
        # becomes the running total only when this seed's constraints are kept.
        # A discarded seed therefore shows a large new_c against an unchanged
        # total_constraints, which is the signature of the discard, and both the
        # bail-out and the resume path read the kept total.
        if keep:
            total_constraints = results[-1]["total_constraints"]
        iterations = len(results)
        trains_completed = results[-1]["trains_completed"]
        trains_per_hr = results[-1]["trains_per_hr"]
        cpu = sum(r["sim_run_time"] for r in results)
        cpu_total += cpu
        # A seed is clean only when no iteration reported a deadlock and none
        # reported an unresolved stall. Until 2026-10-19 this was classified on
        # constraint count, which calls a seed clean whenever a deadlock yielded
        # no constraint, and that is the one case the stabilisation window must
        # not absorb: the monitor can detect a deadlock whose core has no
        # self-contained subset, and the analyser can decline to name a cause.
        # In both the constraint set is unchanged while the run did not drain.
        # Such a seed is recorded as deadlock_found=1 with new_constraints=0.
        # run_incomplete is the drain tripwire: with a drain configured, a run
        # that ends without the network clearing has not completed, and the
        # monitor cannot see it, since a train holding track with no outstanding
        # request trips neither the request timeout nor the stall test. It is
        # always 0 when no drain is configured.
        anomalous = [r for r in results
                     if not r["deadlock_found"] and not r.get("stalled", 0)
                     and r.get("run_incomplete", 0)]
        deadlock_found = 1 if anomalous or any(
            r["deadlock_found"] or r.get("stalled", 0) for r in results) else 0

        if new_c > 0 and not keep:
            print(f"[{label}] seed {seed}: +{new_c}c DISCARDED "
                  f"(never resolved; policy=discard)")
        if new_c > 0 and keep:
            gen_file = os.path.join(output_dir, "generated_constraints.csv")
            if os.path.exists(gen_file):
                shutil.copy2(gen_file, warm_file)
                shutil.copy2(gen_file, constraints_result)
            # The exe opens ./output/scc_log.csv fresh per seed and the harness
            # reuses output/, so it is overwritten unless kept here. Only a
            # generating seed has anything in it.
            scc_src = os.path.join(output_dir, "scc_log.csv")
            if os.path.exists(scc_src):
                scc_dir = os.path.join(
                    os.path.dirname(constraints_result), "scc_logs")
                os.makedirs(scc_dir, exist_ok=True)
                shutil.copy2(scc_src,
                             os.path.join(scc_dir, f"seed{seed:06d}.csv"))
            training_seeds_count += 1
        if deadlock_found:
            consecutive_clean = 0
        else:
            consecutive_clean += 1

        append_csv_row(training_log,
            ["seed", "deadlock_found", "new_constraints", "total_constraints",
             "iterations", "trains_completed", "trains_per_hr", "cpu_time"],
            [seed, deadlock_found, new_c, total_constraints,
             iterations, trains_completed, f"{trains_per_hr:.4f}", f"{cpu:.3f}"])
        if deadlock_found and new_c == 0:
            status = "DEADLOCK, no constraint"
        else:
            status = "CLEAN" if new_c == 0 else f"+{new_c}c"
        if anomalous:
            print(f"[{label}] WARNING: seed {seed} never cleared the network at the drain "
                  f"horizon and no deadlock was detected. Treated as unclean. This is a bug "
                  f"signature, not an expected outcome.")
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
    benchmark_path = os.path.join(sdir, f"hybrid_benchmark{BENCH_SUFFIX}.csv")
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

    for s in BENCH_SEEDS:
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
        failed, clear = bench_outcome(r)
        if failed:
            deadlocks += 1
        append_csv_row(benchmark_path,
            ["seed", "throughput", "cpu_time", "deadlocked",
             "avg_wait_time", "avg_spawn_delay", "drained_clear"],
            [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
             failed,
             f"{r['avg_wait_time']:.6f}", f"{r['avg_spawn_delay']:.6f}", clear])
    print(f"[{label}] Hybrid bench done: {deadlocks} deadlocks/{BENCHMARK_SEEDS}")


# === Phase 4: Engineered Benchmark (PL + engineered, no learned) ===

def run_engineered_benchmark(N, fraction):
    label = scenario_label(N, fraction)
    sdir = scenario_dir(N, fraction)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(sdir, f"engineered_benchmark{BENCH_SUFFIX}.csv")
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
        for s in BENCH_SEEDS:
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
            failed, clear = bench_outcome(r)
            if failed:
                deadlocks += 1
            append_csv_row(benchmark_path,
                ["seed", "throughput", "cpu_time", "deadlocked",
                 "avg_wait_time", "avg_spawn_delay", "drained_clear"],
                [s, f"{r['trains_per_hr']:.4f}", f"{r['sim_run_time']:.3f}",
                 failed,
                 f"{r['avg_wait_time']:.6f}", f"{r['avg_spawn_delay']:.6f}", clear])
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


# A crashed scenario must not look like a finished one: the PBS harness writes a
# done marker on exit 0, so swallowing the exception here records 30 cells as
# complete with nothing in them, which is what happened on 2026-10-19.
def wrapper(args):
    """Returns the label of a scenario that crashed, or None if it finished."""
    N, fraction, skip_training = args
    try:
        run_scenario(N, fraction, skip_training=skip_training)
        return None
    except Exception as e:
        print(f"[{scenario_label(N, fraction)}] CRASH: {e}")
        return scenario_label(N, fraction)


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
    p.add_argument("--phase", choices=["all", "engineered", "benchmarks"], default="all",
                   help="engineered runs phase 4 alone, for re-benchmarking a "
                        "changed spawn-hold set without retraining; benchmarks "
                        "runs phases 1, 3 and 4 on the trained set, for a new "
                        "seed range (BENCH_SEED_START)")
    p.add_argument("--arms", nargs="+", choices=["pl", "hybrid", "engineered"],
                   default=["pl", "hybrid", "engineered"],
                   help="with --phase benchmarks, which benchmarks to run")
    p.add_argument("--policy", choices=["bank", "discard"],
                   default=os.environ.get("STALL_POLICY", "bank"),
                   help="what to do with a replication that never resolved; "
                        "both arms are run and compared, and the policy is "
                        "part of the cell path")
    args = p.parse_args()
    os.environ["STALL_POLICY"] = args.policy

    scenarios = parse_scenarios(args.scenarios)
    print(f"Overlength rerun: {len(scenarios)} scenarios | jobs={args.jobs} | "
          f"lambda={SPAWN_RATE} | MAX_SEEDS={MAX_SEEDS} | MAX_CONSTRAINTS={MAX_CONSTRAINTS}")

    print("Setting up scenario directories...")
    for N, fraction in scenarios:
        setup_scenario(N, fraction)
    print(f"Setup complete: {len(scenarios)} scenarios under {WORK_DIR}")

    if args.setup_only:
        return 0

    if args.phase == "benchmarks":
        for N, fraction in scenarios:
            if "pl" in args.arms:
                run_pl_benchmark(N, fraction)
            if "hybrid" in args.arms:
                run_hybrid_benchmark(N, fraction)
            if "engineered" in args.arms:
                run_engineered_benchmark(N, fraction)
        print(f"Benchmarks complete, seeds {BENCH_SEEDS.start} to {BENCH_SEEDS.stop - 1}.")
        return 0

    if args.phase == "engineered":
        for N, fraction in scenarios:
            run_engineered_benchmark(N, fraction)
        print("Engineered benchmark complete.")
        return 0

    work = [(N, frac, args.skip_training) for N, frac in scenarios]
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            crashed = [c for c in ex.map(wrapper, work) if c]
    else:
        crashed = [c for c in (wrapper(w) for w in work) if c]

    if crashed:
        print(f"Overlength campaign FAILED: {len(crashed)} of {len(work)} scenarios "
              f"crashed ({', '.join(crashed)})")
        return 1
    print("Overlength campaign complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
