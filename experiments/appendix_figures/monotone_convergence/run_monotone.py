#!/usr/bin/env python3
"""
Appendix C (fig:monotone) regeneration: within-seed monotone convergence.

Reproduces the fullbacker N=10, phi=0.25 overlength training run, but preserves
EACH seed's per-iteration dlexp_log.csv so that deadlock detection time can be
plotted against iteration number (Appendix~C). Uses the bundle's current exe
(full-backer fix) and the exact configs_overlength/N10 config, so the trajectory
matches results/overlength/N10/f0.25 (3 deadlock-finding seeds; 4,687 constraints).

Only the deadlock-finding seeds carry a within-seed trace; the run stops after a
short clean streak (CLEAN_STOP) once those are captured -- the full 100-clean
convergence was already established by the campaign.

Output: ./logs/dlexp_seed_XXXX.csv (per-seed traces) + training_summary.csv.
Then:   python plot_monotone.py --logs logs --save monotone_convergence.pdf

Usage:  python run_monotone.py
"""
import csv
import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))          # .../appendix_figures/monotone_convergence
CAMPAIGN_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))  # the bundle
sys.path.insert(0, CAMPAIGN_DIR)
sys.path.insert(0, os.path.join(CAMPAIGN_DIR, "runners"))
from run_overlength_exp import (  # noqa: E402
    set_csv_option, parse_dlexp_log, run_exe,
    read_base_spawners, write_overlength_spawners,
)
from run_overlength import ensure_base_config, base_config_dir  # noqa: E402

# Match the fullbacker overlength N10/f0.25 scenario exactly.
N = 10
FRACTION = 0.25
RATE = 2.00
MAX_ITERATIONS = 2000
MAX_SEEDS = 3000
CLEAN_STOP = 3   # stop after this many consecutive clean seeds (deadlock-finding seeds captured)

WORKDIR = os.path.join(SCRIPT_DIR, "work")
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")


def robust_rows(path):
    """Read dlexp_log.csv, skipping any malformed row (e.g. the partial final
    line the exe can emit when it hits the iteration cap, which has an empty
    iteration field)."""
    with open(path, newline="") as fh:
        rd = csv.DictReader(fh)
        fields = rd.fieldnames
        rows = [r for r in rd if (r.get("iteration") or "").strip().isdigit()]
    return fields, rows


def save_rows(fields, rows, dest):
    with open(dest, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def setup():
    ensure_base_config(N)
    input_dir = os.path.join(WORKDIR, "input")
    output_dir = os.path.join(WORKDIR, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    base = os.path.join(base_config_dir(N), "input")
    for fn in os.listdir(base):
        s = os.path.join(base, fn)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(input_dir, fn))

    # PL-only learning: strip any engineered nogood_constr.csv.
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)

    write_overlength_spawners(
        os.path.join(input_dir, "open_loop_spawners.csv"),
        RATE, FRACTION, read_base_spawners(base_config_dir(N)))

    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp, "MAX_ITERATIONS", MAX_ITERATIONS)
    set_csv_option(dlexp, "CONSTRAINT_EVAL", "flat")  # overlength tree-eval bug -> flat (campaign default)
    set_csv_option(dlexp, "WARM_START_FILE", "")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "pl_constraints", 1)
    # PRUNING left at its default (on) to match the published / campaign run.


def main():
    os.makedirs(LOGS_DIR, exist_ok=True)
    setup()
    input_dir = os.path.join(WORKDIR, "input")
    output_dir = os.path.join(WORKDIR, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    warm = os.path.join(WORKDIR, "warm_constraints.csv")
    if os.path.exists(warm):
        os.remove(warm)

    summ = os.path.join(LOGS_DIR, "training_summary.csv")
    with open(summ, "w", newline="") as f:
        csv.writer(f).writerow(
            ["seed", "new_constraints", "total_constraints", "iterations",
             "dl_time_first", "dl_time_last"])

    consecutive_clean = 0
    total = 0
    seed = 1
    print(f"Monotone rerun: N={N}, phi={FRACTION}, rate={RATE}, MAX_ITER={MAX_ITERATIONS}")
    while consecutive_clean < CLEAN_STOP and seed <= MAX_SEEDS:
        set_csv_option(runctrl, "seed", seed)
        set_csv_option(dlexp, "WARM_START_FILE",
                       "warm_constraints.csv" if os.path.exists(warm) else "")
        try:
            run_exe(WORKDIR)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"ERROR seed {seed}: {e}")
            break

        log = os.path.join(output_dir, "dlexp_log.csv")
        if not os.path.exists(log):
            print(f"seed {seed}: no dlexp_log")
            break
        fields, rows = robust_rows(log)
        save_rows(fields, rows, os.path.join(LOGS_DIR, f"dlexp_seed_{seed:04d}.csv"))
        if not rows:
            break
        new_c = sum(int(r["new_constraints"]) for r in rows)
        total = int(rows[-1]["total_constraints"])
        dl_rows = [r for r in rows if int(r["deadlock_found"]) == 1]
        dl_first = float(dl_rows[0]["deadlock_time"]) if dl_rows else ""
        dl_last = float(dl_rows[-1]["deadlock_time"]) if dl_rows else ""

        if new_c > 0:
            gen = os.path.join(output_dir, "generated_constraints.csv")
            if os.path.exists(gen):
                shutil.copy2(gen, warm)
            consecutive_clean = 0
        else:
            consecutive_clean += 1

        with open(summ, "a", newline="") as f:
            csv.writer(f).writerow(
                [seed, new_c, total, len(rows), dl_first, dl_last])
        status = "CLEAN" if new_c == 0 else f"+{new_c}c"
        print(f"  seed {seed:3d}: {status:8s} total={total:5d}c iters={len(rows):4d} "
              f"dl_time {dl_first}->{dl_last}  clean={consecutive_clean}/{CLEAN_STOP}")
        seed += 1

    print(f"\nDone: {total} constraints, {seed-1} seeds. Per-seed logs in {LOGS_DIR}")
    print("Plot:  python plot_monotone.py --logs logs --save monotone_convergence.pdf")


if __name__ == "__main__":
    main()
