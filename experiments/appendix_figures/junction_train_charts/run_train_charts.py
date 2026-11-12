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
the train-chart flush + CRLF fixes). Output: out/<mode>/train_chart.csv (columns
time,corridor_id,train,chainage,arc) and out/<mode>/train_metric_log.csv, the per-train
log fig4_delay_table.py takes comment 1.m2's waiting times from.

TWO THINGS CHANGED ON 2026-10-23, both of which this script needed to run at all against
the 2026-10-22 campaign binary:

* **The learned set comes from the campaign, not from tmba_input.** The frozen
  tmba_input/nogood_constr.csv is in the pre-A5 segment-level tuple format, and
  read_generated_constraints now rejects that format by name rather than guess a route it
  never recorded, so a run started with it fails outright. --nogood defaults to the
  rerun's own constraints.csv. Its 5 constraints are the same 5, same geometry, as the
  frozen file: checked tuple by tuple, and identical across all seven swept rates and
  both stall policies.
* **The seed is an argument and its value was chosen, not inherited.** Commit f672bc0
  holds a rising speed limit until the train's tail clears the zone, which its own message
  records as changing results on the deadlock-paper datasets, so the published panels
  cannot be reproduced on this binary at any seed. find_figure4_replication.py searches
  for a replication showing the same situation and records the criteria it was chosen by.

The drain is applied as the rest of the campaign applies it, which puts the stats window
end at 192 h. The figure plots the first PLOT_H hours of that, where the network is filling
and the deadlock forms; the delay table is taken over the window. They answer different
questions and the response says so.

Usage:
    python run_train_charts.py                 # both runs, chosen seed
    python run_train_charts.py --seed 15 --mode before
    python run_train_charts.py --no-drain --horizon 96    # the pre-2026-10-23 setup
"""
import argparse
import csv
import os
import re
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))   # 2026-07-05_bugfix_rerun
sys.path.insert(0, CAMPAIGN_DIR)
from _run_toowoomba_helpers import REPO_ROOT, set_csv_option, set_toowoomba_rates  # noqa: E402

LINUX_EXE = os.path.join(REPO_ROOT, "DesRail", "desrail")    # repo's Linux build (make -C DesRail)
TMBA_INPUT = os.path.join(CAMPAIGN_DIR, "tmba_input")        # frozen Toowoomba config (bundle-local)

B_RATE = 1.75
C_RATE = 0.75
# The replication find_figure4_replication.py chose, with the criteria and the runners-up
# recorded in fig4_search/candidates.csv beside it.
SEED = 1
EXE_TIMEOUT = 1800
PLOT_H = 10.0
# Drain, as run_heldout_drained.py sets it.
BASE_SIM_LEN = 192.0
DRAIN_HORIZON = 1000.0
DEFAULT_NOGOOD = os.path.join(CAMPAIGN_DIR, "results", "toowoomba", "discard", "hybrid",
                              "b0.80_c0.50", "constraints.csv")


def setup(mode, horizon, seed, nogood, drain):
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

    # AFTER gets the learned junction set (nogood_constr.csv, auto-loaded by
    # single_replicate, main.cpp:20); BEFORE must not have one, and must not be left
    # holding the frozen pre-A5 file either, since the reader rejects that format.
    dst = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(dst):
        os.remove(dst)
    if mode == "after":
        shutil.copy2(nogood, dst)

    set_toowoomba_rates(os.path.join(input_dir, "open_loop_spawners.csv"),
                        B_RATE, C_RATE)

    runctrl = os.path.join(input_dir, "runctrl.csv")
    if drain:
        set_csv_option(runctrl, "sim_len", DRAIN_HORIZON)
        rows = list(csv.reader(open(runctrl, newline="")))
        if any(r and r[0] == "start_warmdown" for r in rows):
            set_csv_option(runctrl, "start_warmdown", BASE_SIM_LEN)
        else:
            rows.append(["start_warmdown", str(BASE_SIM_LEN), ""])
            with open(runctrl, "w", newline="") as f:
                csv.writer(f).writerows(rows)
    else:
        set_csv_option(runctrl, "sim_len", horizon)
    set_csv_option(runctrl, "warmup", 0)
    set_csv_option(runctrl, "seed", seed)
    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(runctrl, "train_charts", 1)      # <-- emit train_chart.csv
    set_csv_option(runctrl, "animate", 0)
    set_csv_option(runctrl, "screen_output", 0)

    set_csv_option(os.path.join(input_dir, "main_option.csv"),
                   "enter experiment type", "single_replicate")
    return sdir, output_dir


def run(mode, horizon, seed, nogood, drain, exe):
    sdir, output_dir = setup(mode, horizon, seed, nogood, drain)
    n_constr = 0
    ng = os.path.join(sdir, "input", "nogood_constr.csv")
    if os.path.exists(ng):
        with open(ng) as f:
            n_constr = len({ln.split(",")[0] for ln in f
                            if ln.startswith("dl_")})
    print(f"[{mode}] b={B_RATE} c={C_RATE} seed={seed} "
          f"{'drained' if drain else f'horizon={horizon}'} "
          f"junction_constraints={n_constr}")
    with open(os.path.join(output_dir, "stdout.log"), "w") as out, \
         open(os.path.join(output_dir, "stderr.log"), "w") as err:
        r = subprocess.run([exe], cwd=sdir, stdin=subprocess.DEVNULL,
                           stdout=out, stderr=err, timeout=EXE_TIMEOUT)
    if r.returncode != 0:
        tail = open(os.path.join(output_dir, "stderr.log")).read()[-500:]
        raise RuntimeError(f"[{mode}] desrail rc={r.returncode}: {tail}")

    # The exe says how many it loaded. Assert it rather than assume the file parsed:
    # a constraint file in a format the reader will not take is the difference between
    # this figure's two panels, and a silent zero would make them the same run.
    if mode == "after":
        log = open(os.path.join(output_dir, "stdout.log")).read()
        m = re.search(r"Loaded (\d+) constraints", log)
        if not m or int(m.group(1)) != n_constr:
            raise RuntimeError(f"[{mode}] expected {n_constr} constraints loaded, "
                               f"stdout says {m.group(1) if m else 'nothing'}")
        print(f"[{mode}] confirmed {m.group(1)} constraints loaded")

    out_dir = os.path.join(SCRIPT_DIR, "out", mode)
    os.makedirs(out_dir, exist_ok=True)
    for name in ("train_chart.csv", "train_metric_log.csv"):
        src = os.path.join(output_dir, name)
        if os.path.exists(src):
            dst = os.path.join(out_dir, name)
            shutil.copy2(src, dst)
            n = sum(1 for _ in open(src)) - 1
            print(f"[{mode}] -> {dst}  ({n} rows)")
        else:
            print(f"[{mode}] WARNING: no {name} produced in {output_dir}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--horizon", type=float, default=96.0,
                   help="only used with --no-drain")
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--nogood", default=DEFAULT_NOGOOD)
    p.add_argument("--no-drain", dest="drain", action="store_false",
                   help="the pre-2026-10-23 setup: fixed horizon, no warmdown")
    p.add_argument("--exe", default=os.environ.get("DESRAIL_EXE") or LINUX_EXE)
    p.add_argument("--mode", choices=["before", "after", "both"], default="both")
    args = p.parse_args()

    if not os.path.isfile(args.exe):
        sys.exit(f"ERROR: {args.exe} not found (build the bundle Linux binary first)")
    if not os.path.isfile(args.nogood):
        sys.exit(f"ERROR: learned set not found: {args.nogood}")
    md5 = subprocess.run(["md5sum", args.exe], capture_output=True, text=True).stdout.split()[0]
    print(f"exe {args.exe}\nexe_md5 {md5}\nlearned_set {args.nogood}")

    modes = ["before", "after"] if args.mode == "both" else [args.mode]
    for m in modes:
        run(m, args.horizon, args.seed, args.nogood, args.drain, args.exe)


if __name__ == "__main__":
    main()
