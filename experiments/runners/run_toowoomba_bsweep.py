#!/usr/bin/env python3
"""
Toowoomba b-sweep driver (supersedes the 4x3 b/c grid for the junction experiment).

Fixes corridor route rate c = 0.50 and sweeps Brisbane route rate
b in {0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80}, running TWO training modes per b:

  * pure   : pl_constraints = 0  (learner must discover everything; finds the
             pure-learning convergence boundary as b rises)
  * hybrid : pl_constraints = 1  (standard PL constraints active; learner only
             needs the residual junction constraints -> converges quickly)

Training only (no benchmark, per design). Resumable: re-reads training_log.csv.
Bail-out matches the rest of the campaign: seeds > 3000 OR constraints > 25000.

Output: work/toowoomba_bsweep/<mode>/b{b}_c0.50/ ; lean training_log.csv +
constraints.csv are also copied to results/toowoomba/<mode>/b{b}_c0.50/.

Usage:
    python run_toowoomba_bsweep.py                 # both modes, all b, jobs=12
    python run_toowoomba_bsweep.py --jobs 8
    python run_toowoomba_bsweep.py --modes hybrid  # one mode only
    python run_toowoomba_bsweep.py --setup-only
"""
import argparse
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, CAMPAIGN_DIR)
from _run_toowoomba_helpers import (  # noqa: E402
    set_csv_option, set_toowoomba_rates, parse_dlexp_log,
    load_csv_log, run_exe, append_csv_row,
)

def _find_repo_root(start):
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
TMBA_INPUT = os.path.join(REPO_ROOT, "DesRail", "input")
WORK_ROOT = os.path.join(CAMPAIGN_DIR, "work", "toowoomba_bsweep")
RESULTS_ROOT = os.path.join(CAMPAIGN_DIR, "results", "toowoomba")

B_RATES = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
C_RATE = 0.50
WARMUP = 24
HORIZON = 168 + WARMUP
CLEAN_THRESHOLD = 100
MAX_SEEDS = 3000
MAX_CONSTRAINTS = 25000
MAX_ITERATIONS_TRAINING = 2000

PL = {"pure": 0, "hybrid": 1}


def sdir_for(mode, b):
    return os.path.join(WORK_ROOT, mode, f"b{b:.2f}_c{C_RATE:.2f}")


def setup(mode, b):
    sdir = sdir_for(mode, b)
    input_dir = os.path.join(sdir, "input")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(os.path.join(sdir, "output"), exist_ok=True)
    for fname in os.listdir(TMBA_INPUT):
        src = os.path.join(TMBA_INPUT, fname)
        if os.path.isfile(src) and fname.endswith(".csv"):
            dst = os.path.join(input_dir, fname)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
    # blank manual constraints (learner discovers junction; PL via flag)
    with open(os.path.join(input_dir, "signal_constraints.csv"), "w", newline="") as f:
        f.write("str_id,segment / occ_limit,head,target,comment\n")
    # strip any pre-cooked engineered constraints: the exe auto-loads
    # input/nogood_constr.csv (deadlock_avoidance.cpp:1048); a stale copy from
    # DesRail/input would pre-prevent the junction deadlock and the learner would
    # find nothing. Training must start with no engineered constraints.
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)
    set_toowoomba_rates(os.path.join(input_dir, "open_loop_spawners.csv"), b, C_RATE)
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "sim_len", HORIZON)
    set_csv_option(runctrl, "warmup", WARMUP)
    set_csv_option(runctrl, "animate", 0)
    set_csv_option(runctrl, "screen_output", 0)
    set_csv_option(runctrl, "log_output", 0)
    set_csv_option(runctrl, "pl_constraints", PL[mode])
    set_csv_option(os.path.join(input_dir, "main_option.csv"),
                   "enter experiment type", "deadlock_avoidance_exp")
    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp, "CONSTRAINT_EVAL", "tree")
    set_csv_option(dlexp, "START_DEBUG", 99999)
    set_csv_option(dlexp, "WARM_START_FILE", "")
    set_csv_option(dlexp, "LAST_CONSTRAINT", "")
    set_csv_option(dlexp, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    return sdir


def train(mode, b):
    label = f"{mode}/b{b:.2f}"
    sdir = setup(mode, b)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(sdir, "training_log.csv")
    constraints_result = os.path.join(sdir, "constraints.csv")
    warm_file = os.path.join(sdir, "warm_constraints.csv")

    set_csv_option(runctrl, "pl_constraints", PL[mode])
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
    total_constraints = int(existing[-1]["total_constraints"]) if existing else 0
    if consecutive_clean >= CLEAN_THRESHOLD:
        print(f"[{label}] already converged: {total_constraints}c")
        return

    bail = None
    while consecutive_clean < CLEAN_THRESHOLD and seed <= MAX_SEEDS:
        if total_constraints > MAX_CONSTRAINTS:
            bail = "max_constraints"; break
        set_csv_option(runctrl, "seed", seed)
        set_csv_option(dlexp, "WARM_START_FILE",
                       "warm_constraints.csv" if os.path.exists(warm_file) else "")
        try:
            run_exe(sdir)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"[{label}] ERROR seed {seed}: {e}"); bail = "exe_error"; break
        results = parse_dlexp_log(os.path.join(output_dir, "dlexp_log.csv"))
        if not results:
            bail = "empty_log"; break
        new_c = sum(r["new_constraints"] for r in results)
        total_constraints = results[-1]["total_constraints"]
        iterations = len(results)
        trains_completed = results[-1]["trains_completed"]
        trains_per_hr = results[-1]["trains_per_hr"]
        cpu = sum(r["sim_run_time"] for r in results)
        if new_c > 0:
            gen = os.path.join(output_dir, "generated_constraints.csv")
            if os.path.exists(gen):
                shutil.copy2(gen, warm_file); shutil.copy2(gen, constraints_result)
            consecutive_clean = 0
        else:
            consecutive_clean += 1
        append_csv_row(training_log,
            ["seed", "deadlock_found", "new_constraints", "total_constraints",
             "iterations", "trains_completed", "trains_per_hr", "cpu_time"],
            [seed, 1 if new_c > 0 else 0, new_c, total_constraints,
             iterations, trains_completed, f"{trains_per_hr:.4f}", f"{cpu:.3f}"])
        if seed % 25 == 0 or new_c > 0:
            print(f"[{label}] seed {seed}: "
                  f"{'+'+str(new_c)+'c' if new_c else 'CLEAN'} "
                  f"(total={total_constraints}c, {consecutive_clean}/{CLEAN_THRESHOLD})")
        seed += 1
    if bail is None:
        bail = "converged" if consecutive_clean >= CLEAN_THRESHOLD else "max_seeds"
    print(f"[{label}] done ({bail}): {total_constraints}c, {seed - 1} seeds")
    # harvest lean files
    rdir = os.path.join(RESULTS_ROOT, mode, f"b{b:.2f}_c{C_RATE:.2f}")
    os.makedirs(rdir, exist_ok=True)
    if os.path.exists(training_log):
        shutil.copy2(training_log, os.path.join(rdir, "training_log.csv"))
    if os.path.exists(constraints_result):
        shutil.copy2(constraints_result, os.path.join(rdir, "constraints.csv"))


def worker(spec):
    mode, b = spec
    try:
        train(mode, b)
    except Exception as e:
        print(f"[{mode}/b{b:.2f}] CRASH: {e}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=12)
    p.add_argument("--modes", nargs="+", default=["pure", "hybrid"], choices=["pure", "hybrid"])
    p.add_argument("--bvals", nargs="+", type=float, default=B_RATES)
    p.add_argument("--setup-only", action="store_true")
    args = p.parse_args()

    specs = [(m, b) for m in args.modes for b in args.bvals]
    print(f"Toowoomba b-sweep: modes={args.modes} b={args.bvals} c={C_RATE} "
          f"| {len(specs)} runs | jobs={args.jobs}")
    for m, b in specs:
        setup(m, b)
    if args.setup_only:
        print("setup-only done."); return 0
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            list(ex.map(worker, specs))
    else:
        for s in specs:
            worker(s)
    print("b-sweep complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
