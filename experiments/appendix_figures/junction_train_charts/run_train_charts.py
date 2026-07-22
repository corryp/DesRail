#!/usr/bin/env python3
"""
Toowoomba junction before/after train charts (replaces the throughput table in the
deadlock paper's Toowoomba results section).

Two single-replicate runs of the SAME scenario (lambda_B=1.75, lambda_C=0.75, seed 1,
PL constraints ON), differing only in whether the learned junction constraints are
loaded (this bundle's tmba_input/nogood_constr.csv, the 5-constraint set):

  BEFORE : nogood_constr.csv removed  -> the junction deadlocks; trains pile up / stall.
  AFTER  : nogood_constr.csv kept      -> free-flowing traffic.

single_replicate is used deliberately (NOT deadlock_avoidance_exp): its DeadlockMonitor
would set max_time = detection time and truncate the run at the deadlock (~t=8), cutting
off the flatline. single_replicate has no monitor, so the sim runs until the network
gridlocks (event queue empties) and stalled trains show as flat lines to that wall
(relies on the TrainChartTrace end-of-run flush in rail_sim_tools.cpp).

Runs the repo's LINUX binary DesRail/desrail (build it with `make -C DesRail`; it carries
the train-chart flush + CRLF fixes). Output:
out/<mode>/train_chart.csv (columns time,corridor_id,train,chainage,arc).

Usage:
    python run_train_charts.py                 # both runs, default horizon
    python run_train_charts.py --horizon 72
    python run_train_charts.py --mode before   # single mode
"""
import argparse
import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))   # the experiments/ folder
sys.path.insert(0, CAMPAIGN_DIR)
from _run_toowoomba_helpers import REPO_ROOT, set_csv_option, set_toowoomba_rates  # noqa: E402

LINUX_EXE = os.path.join(REPO_ROOT, "DesRail", "desrail")    # repo's Linux build (make -C DesRail)
TMBA_INPUT = os.path.join(CAMPAIGN_DIR, "tmba_input")        # frozen Toowoomba config (bundle-local)

B_RATE = 1.75
C_RATE = 0.75
SEED = 1
EXE_TIMEOUT = 1800


def setup(mode, horizon):
    sdir = os.path.join(SCRIPT_DIR, "work", mode)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    for fn in os.listdir(TMBA_INPUT):
        s = os.path.join(TMBA_INPUT, fn)
        if os.path.isfile(s) and fn.endswith(".csv"):
            shutil.copy2(s, os.path.join(input_dir, fn))

    # No manual constraints; PL supplied via flag.
    with open(os.path.join(input_dir, "signal_constraints.csv"), "w", newline="") as f:
        f.write("str_id,segment / occ_limit,head,target,comment\n")

    # AFTER keeps the bundle's learned junction set (nogood_constr.csv, auto-loaded by
    # single_replicate, main.cpp:20); BEFORE removes it so the junction deadlocks.
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if mode == "before" and os.path.exists(nogood):
        os.remove(nogood)

    set_toowoomba_rates(os.path.join(input_dir, "open_loop_spawners.csv"),
                        B_RATE, C_RATE)

    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "sim_len", horizon)
    set_csv_option(runctrl, "warmup", 0)
    set_csv_option(runctrl, "seed", SEED)
    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(runctrl, "train_charts", 1)      # <-- emit train_chart.csv
    set_csv_option(runctrl, "animate", 0)
    set_csv_option(runctrl, "screen_output", 0)

    set_csv_option(os.path.join(input_dir, "main_option.csv"),
                   "enter experiment type", "single_replicate")
    return sdir, output_dir


def run(mode, horizon):
    sdir, output_dir = setup(mode, horizon)
    n_constr = 0
    ng = os.path.join(sdir, "input", "nogood_constr.csv")
    if os.path.exists(ng):
        with open(ng) as f:
            n_constr = len({ln.split(",")[0] for ln in f
                            if ln.startswith("dl_")})
    print(f"[{mode}] b={B_RATE} c={C_RATE} seed={SEED} horizon={horizon} "
          f"junction_constraints={n_constr}")
    with open(os.path.join(output_dir, "stdout.log"), "w") as out, \
         open(os.path.join(output_dir, "stderr.log"), "w") as err:
        r = subprocess.run([LINUX_EXE], cwd=sdir, stdin=subprocess.DEVNULL,
                           stdout=out, stderr=err, timeout=EXE_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"[{mode}] desrail rc={r.returncode}; see {output_dir}/stderr.log")

    tc = os.path.join(output_dir, "train_chart.csv")
    out_dir = os.path.join(SCRIPT_DIR, "out", mode)
    os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(tc):
        dst = os.path.join(out_dir, "train_chart.csv")
        shutil.copy2(tc, dst)
        n = sum(1 for _ in open(tc)) - 1
        print(f"[{mode}] -> {dst}  ({n} sample rows)")
    else:
        print(f"[{mode}] WARNING: no train_chart.csv produced in {output_dir}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--horizon", type=float, default=96.0)
    p.add_argument("--mode", choices=["before", "after", "both"], default="both")
    args = p.parse_args()

    if not os.path.isfile(LINUX_EXE):
        sys.exit(f"ERROR: {LINUX_EXE} not found (build the bundle Linux binary first)")

    modes = ["before", "after"] if args.mode == "both" else [args.mode]
    for m in modes:
        run(m, args.horizon)


if __name__ == "__main__":
    main()
