#!/usr/bin/env python3
"""
Toowoomba junction-network experiment driver for the 2026-05-20 pruning rerun.

Replicates the paper's Section 6.3 experiment: 4 Brisbane route rates
(0.25, 0.75, 1.25, 1.75) x 3 corridor route rates (0.25, 0.50, 0.75) = 12 scenarios.

Per scenario:
  1. Training: PL constraints + manual engineered constraints + iterative learning.
  2. Benchmark: 100 seeds with learned constraints vs manual engineered constraints
     (same protocol as the original run_toowoomba.py).

Rectified bail-out: terminate training when seeds > 3000 OR constraints > 25000.

Folder structure:
    toowoomba/
        run_toowoomba.py
        tmba_manual_constraints.csv   (copy of experiments/tmba_manual_constraints.csv)
        b{X.XX}_c{X.XX}/
            input/                    scenario config
            output/                   last seed's exe output
            training_log.csv          per-seed training log
            constraints.csv           final learned constraints
            warm_constraints.csv      working warm-start file
            benchmark.csv             learned vs manual benchmark

Usage:
    python run_toowoomba.py
    python run_toowoomba.py --scenarios b0.75:c0.50
    python run_toowoomba.py --setup-only
    python run_toowoomba.py --skip-training
    python run_toowoomba.py --skip-benchmark
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))      # .../runners
CAMPAIGN_DIR = os.path.dirname(SCRIPT_DIR)                   # the set folder
SET_LABEL = "toowoomba"
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

# Helpers live in CAMPAIGN_DIR as `_run_toowoomba_helpers.py` (renamed to
# avoid colliding with this script's own filename when both are on sys.path).
sys.path.insert(0, CAMPAIGN_DIR)
from _run_toowoomba_helpers import (  # noqa: E402
    set_csv_option,
    set_toowoomba_rates,
    parse_dlexp_log,
    load_csv_log,
    run_exe,
    apply_drain,
    append_csv_row,
    set_or_add_csv_option,
)

TMBA_INPUT = os.path.join(CAMPAIGN_DIR, "tmba_input")   # frozen Toowoomba config (bundle-local)
SOURCE_MANUAL_CONSTRAINTS = os.path.join(SCRIPT_DIR, "tmba_manual_constraints.csv")
LOCAL_MANUAL_CONSTRAINTS = os.path.join(SCRIPT_DIR, "tmba_manual_constraints.csv")
# DESRAIL_EXE points at the native binary for a Linux or HPC run; unset, this
# is the Windows build the laptop uses, so nothing already configured moves.
EXE_PATH = os.environ.get("DESRAIL_EXE") or os.path.join(
    REPO_ROOT, "x64", "Release", "DesRail.exe")

# === Campaign parameters ===

BRISBANE_RATES = [0.25, 0.75, 1.25, 1.75]
CORRIDOR_RATES = [0.25, 0.50, 0.75]
WARMUP = 24
HORIZON = 168 + WARMUP
CLEAN_THRESHOLD = 100
MAX_SEEDS = 3000
MAX_CONSTRAINTS = 25000
MAX_ITERATIONS_TRAINING = 2000
BENCHMARK_SEEDS = 100


def rate_label(b, c):
    return f"b{b:.2f}_c{c:.2f}"


def scenario_label(b, c):
    return rate_label(b, c)


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


def scenario_dir(b, c):
    return os.path.join(WORK_DIR, stall_policy(), rate_label(b, c))


def setup_scenario(b, c):
    """Create scenario subdir and populate input/ from tmba_input + rates."""
    sdir = scenario_dir(b, c)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # Copy Toowoomba input files
    for fname in os.listdir(TMBA_INPUT):
        src = os.path.join(TMBA_INPUT, fname)
        if os.path.isfile(src) and fname.endswith(".csv"):
            dst = os.path.join(input_dir, fname)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)

    # Blank signal_constraints.csv for training (manual constraints used at benchmark time)
    sc_path = os.path.join(input_dir, "signal_constraints.csv")
    with open(sc_path, "w", newline="") as f:
        f.write("str_id,segment / occ_limit,head,target,comment\n")

    # Spawn rates per ols name
    set_toowoomba_rates(
        os.path.join(input_dir, "open_loop_spawners.csv"), b, c)

    # Runctrl + main_option + dlexp_opts for training
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "sim_len", HORIZON)
    set_csv_option(runctrl, "warmup", WARMUP)
    set_csv_option(runctrl, "animate", 0)
    set_csv_option(runctrl, "screen_output", 0)
    set_csv_option(runctrl, "log_output", 0)
    set_csv_option(runctrl, "pl_constraints", 0)
    apply_drain(runctrl)   # horizon = HORIZON set above; detect late deadlocks in training

    main_opt = os.path.join(input_dir, "main_option.csv")
    set_csv_option(main_opt, "enter experiment type", "deadlock_avoidance_exp")

    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "tree")  # ignored — flat only
    set_csv_option(dlexp_opts, "START_DEBUG", 99999)
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")
    set_csv_option(dlexp_opts, "LAST_CONSTRAINT", "")
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)

    return sdir


def ensure_local_manual_constraints():
    """Make a local copy of tmba_manual_constraints.csv so the rerun is self-contained."""
    if not os.path.exists(LOCAL_MANUAL_CONSTRAINTS) and os.path.exists(SOURCE_MANUAL_CONSTRAINTS):
        shutil.copy2(SOURCE_MANUAL_CONSTRAINTS, LOCAL_MANUAL_CONSTRAINTS)


# === Training ===

def run_training(b, c):
    label = scenario_label(b, c)
    sdir = scenario_dir(b, c)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(sdir, "training_log.csv")
    constraints_result = os.path.join(sdir, "constraints.csv")
    warm_file = os.path.join(sdir, "warm_constraints.csv")

    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    set_csv_option(runctrl, "pl_constraints", 0)

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

    if consecutive_clean >= CLEAN_THRESHOLD:
        print(f"[{label}] Training already converged: {total_constraints}c")
        return

    bail_reason = None
    while consecutive_clean < CLEAN_THRESHOLD and seed <= MAX_SEEDS:
        if total_constraints > MAX_CONSTRAINTS:
            bail_reason = "max_constraints"
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


# === Benchmark (learned vs manual) ===

def run_benchmark(b, c):
    label = scenario_label(b, c)
    sdir = scenario_dir(b, c)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    benchmark_path = os.path.join(sdir, "benchmark.csv")
    warm_file = os.path.join(sdir, "warm_constraints.csv")
    constraints_result = os.path.join(sdir, "constraints.csv")

    if not os.path.exists(warm_file) and os.path.exists(constraints_result):
        shutil.copy2(constraints_result, warm_file)

    set_csv_option(dlexp_opts, "MAX_ITERATIONS", 1)

    _, done = load_csv_log(benchmark_path)
    bench_seeds = list(range(1, BENCHMARK_SEEDS + 1))
    has_constraints = os.path.exists(warm_file)

    for i, s in enumerate(bench_seeds):
        if s in done:
            continue
        set_csv_option(runctrl, "seed", s)

        # Learned run — blank signal_constraints.csv, learned in warm-start
        sc_path = os.path.join(input_dir, "signal_constraints.csv")
        with open(sc_path, "w", newline="") as f:
            f.write("str_id,segment / occ_limit,head,target,comment\n")
        set_csv_option(runctrl, "pl_constraints", 0)
        set_csv_option(dlexp_opts, "WARM_START_FILE",
                       "warm_constraints.csv" if has_constraints else "")
        try:
            run_exe(sdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] Bench ERROR (learned) seed {s}: {e}")
            continue
        learned = parse_dlexp_log(os.path.join(output_dir, "dlexp_log.csv"))

        # Manual run — load manual constraints, no warm-start
        set_csv_option(runctrl, "pl_constraints", 1)
        if os.path.exists(LOCAL_MANUAL_CONSTRAINTS):
            shutil.copy2(LOCAL_MANUAL_CONSTRAINTS, sc_path)
        set_csv_option(dlexp_opts, "WARM_START_FILE", "")
        try:
            run_exe(sdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] Bench ERROR (manual) seed {s}: {e}")
            continue
        manual = parse_dlexp_log(os.path.join(output_dir, "dlexp_log.csv"))

        if not learned or not manual:
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
             f"{manual[0]['trains_per_hr']:.4f}",
             f"{manual[0]['sim_run_time']:.3f}",
             manual[0]["deadlock_found"],
             f"{manual[0]['avg_wait_time']:.6f}",
             f"{manual[0]['avg_spawn_delay']:.6f}"])
        if (i + 1) % 10 == 0 or i == len(bench_seeds) - 1:
            print(f"[{label}] bench {i + 1}/{len(bench_seeds)}")

    print(f"[{label}] Benchmark complete")


# === Per-scenario driver ===

def run_scenario(b, c, skip_training=False, skip_benchmark=False):
    setup_scenario(b, c)
    if not skip_training:
        run_training(b, c)
    if not skip_benchmark:
        run_benchmark(b, c)


# A crashed scenario must not look like a finished one: the PBS harness writes a
# done marker on exit 0, so swallowing the exception here records the cell as
# complete with nothing in it, which is what happened to overlength on 2026-10-19.
def wrapper(args):
    """Returns the label of a scenario that crashed, or None if it finished."""
    b, c, skip_t, skip_b = args
    try:
        run_scenario(b, c, skip_training=skip_t, skip_benchmark=skip_b)
        return None
    except Exception as e:
        print(f"[{scenario_label(b, c)}] CRASH: {e}")
        return scenario_label(b, c)


# === CLI ===

def parse_scenarios(specs):
    if not specs:
        return [(b, c) for b in BRISBANE_RATES for c in CORRIDOR_RATES]
    out = []
    for spec in specs:
        b_part, c_part = spec.split(":")
        b = float(b_part.lstrip("b"))
        c = float(c_part.lstrip("c"))
        out.append((b, c))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenarios", nargs="+",
                   help="b{X.XX}:c{X.XX} (default: 12 = 4 b x 3 c)")
    p.add_argument("--jobs", type=int, default=12)
    p.add_argument("--setup-only", action="store_true")
    p.add_argument("--skip-training", action="store_true")
    p.add_argument("--skip-benchmark", action="store_true")
    p.add_argument("--policy", choices=["bank", "discard"],
                   default=os.environ.get("STALL_POLICY", "bank"),
                   help="what to do with a replication that never resolved; "
                        "both arms are run and compared, and the policy is "
                        "part of the cell path")
    args = p.parse_args()
    os.environ["STALL_POLICY"] = args.policy

    ensure_local_manual_constraints()

    scenarios = parse_scenarios(args.scenarios)
    print(f"Toowoomba rerun: {len(scenarios)} scenarios | jobs={args.jobs} | "
          f"MAX_SEEDS={MAX_SEEDS} | MAX_CONSTRAINTS={MAX_CONSTRAINTS}")

    print("Setting up scenario directories...")
    for b, c in scenarios:
        setup_scenario(b, c)
    print(f"Setup complete: {len(scenarios)} scenarios under {WORK_DIR}")

    if args.setup_only:
        return 0

    work = [(b, c, args.skip_training, args.skip_benchmark) for b, c in scenarios]
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            crashed = [c for c in ex.map(wrapper, work) if c]
    else:
        crashed = [c for c in (wrapper(w) for w in work) if c]

    if crashed:
        print(f"Toowoomba campaign FAILED: {len(crashed)} of {len(work)} scenarios "
              f"crashed ({', '.join(crashed)})")
        return 1
    print("Toowoomba campaign complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
