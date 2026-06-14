#!/usr/bin/env python3
"""
Pruning ablation driver for the 2026-05-20 pruning rerun.

Isolates the contribution of redundant constraint edge pruning on the
synthetic corridor. For each cell (N, rate) it runs the training phase twice
under the SAME exe / seeds / network — once with pruning ON (PRUNING=1, the
published method) and once OFF (PRUNING=0) — so the only variable is the
toggle. Supports a short comment in the paper, not a full results table.

Termination per arm (same as the corridor rerun):
  * converge: 100 consecutive clean seeds
  * bail-out: seed count > 3000 OR total constraints > 25000
The no-prune arm is expected to bail out at large N (constraint explosion),
mirroring the Toowoomba behaviour.

Folder structure (relative to this script):
    N{XX}/r{X.XX}/p{0,1}/
        input/                scenario config (auto-populated on first run)
        output/               exe output (overwritten each seed)
        training_log.csv      per-seed training summary
        constraints.csv       final union of learned constraints
        warm_constraints.csv  working warm-start file
    summary.csv               one row per (N, rate, pruning) + on/off table

Usage:
    python run_pruning_ablation.py                       # default cells, --jobs 1
    python run_pruning_ablation.py --jobs 4
    python run_pruning_ablation.py --cells N10:r2.50 N15:r2.50
    python run_pruning_ablation.py --setup-only
    python run_pruning_ablation.py --dry-run

Requires a DesRail.exe built with the PRUNING option.
"""

import argparse
import csv
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

# === Paths (mirrors corridor/run_corridor.py) ===

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))     # .../runners
CAMPAIGN_DIR = os.path.dirname(SCRIPT_DIR)                  # the set folder
SET_LABEL = "pruning_ablation"
# Heavy artifacts live under work/ (VM-only); run_all.py harvests only the lean
# analysis CSVs (training_log.csv + summary.csv) into results/.
WORK_DIR = os.path.join(CAMPAIGN_DIR, "work", SET_LABEL)

# Reuse canonical helpers from run_experiments.py (lives in CAMPAIGN_DIR). Its
# own _find_repo_root resolves EXE_PATH to the rerun dir's exe, so run_exe just
# works without repointing.
sys.path.insert(0, CAMPAIGN_DIR)
from run_experiments import (  # noqa: E402
    set_csv_option,
    set_spawn_rate,
    parse_dlexp_log,
    load_csv_log,
    run_exe,
    append_csv_row,
    rate_label,
    EXE_PATH,
)

# === Parameters ===

CLEAN_THRESHOLD = 100
MAX_SEEDS = 3000
MAX_CONSTRAINTS = 25000
MAX_ITERATIONS_TRAINING = 2000

# Representative cells: vary N at a fixed moderate-high rate to expose pruning's
# effect scaling with corridor size, plus one high-rate cell where constraint
# counts are largest. Edit here or override with --cells.
DEFAULT_CELLS = [(5, 2.50), (10, 2.50), (15, 2.50), (10, 3.00)]
PRUNE_MODES = [1, 0]  # 1 = pruning on (published), 0 = ablated


def label(N, rate, prune):
    return f"N{N:02d}/{rate_label(rate)}/p{prune}"


def set_or_add_option(filepath, option_name, value):
    """Like set_csv_option but appends the row if missing (run_experiments'
    set_csv_option raises). Needed because pre-existing configs lack PRUNING."""
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


def scenario_dir(N, rate, prune):
    return os.path.join(WORK_DIR, f"N{N:02d}", rate_label(rate), f"p{prune}")


# === Setup ===

def setup_scenario(N, rate, prune):
    """Populate a scenario dir from CAMPAIGN_DIR/configs/N{XX}, set the rate,
    pl_constraints=0, tree eval, MAX_ITERATIONS, and the PRUNING toggle."""
    sdir = scenario_dir(N, rate, prune)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    base_input = os.path.join(CAMPAIGN_DIR, "configs", f"N{N:02d}", "input")
    for fname in os.listdir(base_input):
        src = os.path.join(base_input, fname)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(input_dir, fname))

    set_spawn_rate(os.path.join(input_dir, "open_loop_spawners.csv"), rate)

    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "tree")
    set_csv_option(runctrl, "pl_constraints", 0)
    set_or_add_option(dlexp_opts, "PRUNING", prune)

    # Fresh start each arm — no warm-start carryover between modes
    warm_file = os.path.join(sdir, "warm_constraints.csv")
    if os.path.exists(warm_file):
        os.remove(warm_file)
    return sdir


# === Training (one arm) ===

def run_training(N, rate, prune):
    lbl = label(N, rate, prune)
    sdir = scenario_dir(N, rate, prune)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    training_log = os.path.join(sdir, "training_log.csv")
    constraints_result = os.path.join(sdir, "constraints.csv")
    warm_file = os.path.join(sdir, "warm_constraints.csv")

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
    training_seeds = sum(1 for r in existing_rows if int(r["new_constraints"]) > 0)
    cpu_total = sum(float(r.get("cpu_time", 0)) for r in existing_rows)

    def finish(status):
        print(f"[{lbl}] {status}: {total_constraints}c, {seed-1} seeds, "
              f"{training_seeds} training seeds")
        return {"N": N, "rate": rate, "prune": prune,
                "final_constraints": total_constraints, "total_seeds": seed - 1,
                "training_seeds": training_seeds, "status": status,
                "cpu": round(cpu_total, 1)}

    if consecutive_clean >= CLEAN_THRESHOLD:
        return finish("converged")
    if seed > MAX_SEEDS:
        return finish("max_seeds")
    if total_constraints > MAX_CONSTRAINTS:
        return finish("max_constraints")

    while consecutive_clean < CLEAN_THRESHOLD and seed <= MAX_SEEDS:
        if total_constraints > MAX_CONSTRAINTS:
            return finish("max_constraints")

        set_csv_option(runctrl, "seed", seed)
        if os.path.exists(warm_file):
            set_csv_option(dlexp_opts, "WARM_START_FILE", "warm_constraints.csv")
        else:
            set_csv_option(dlexp_opts, "WARM_START_FILE", "")

        try:
            run_exe(sdir)
        except Exception as e:
            print(f"[{lbl}] ERROR at seed {seed}: {e}")
            return finish("error")

        log_path = os.path.join(output_dir, "dlexp_log.csv")
        results = parse_dlexp_log(log_path) if os.path.exists(log_path) else []
        if not results:
            print(f"[{lbl}] ERROR: empty dlexp_log.csv at seed {seed}")
            return finish("error")

        new_constraints = sum(r["new_constraints"] for r in results)
        total_constraints = results[-1]["total_constraints"]
        cpu = sum(r["sim_run_time"] for r in results)
        cpu_total += cpu
        deadlock_found = 1 if new_constraints > 0 else 0

        if new_constraints > 0:
            gen_file = os.path.join(output_dir, "generated_constraints.csv")
            if os.path.exists(gen_file):
                shutil.copy2(gen_file, warm_file)
                shutil.copy2(gen_file, constraints_result)
            consecutive_clean = 0
            training_seeds += 1
        else:
            consecutive_clean += 1

        append_csv_row(training_log,
            ["seed", "deadlock_found", "new_constraints", "total_constraints",
             "iterations", "trains_per_hr", "cpu_time"],
            [seed, deadlock_found, new_constraints, total_constraints,
             len(results), f"{results[-1]['trains_per_hr']:.4f}", f"{cpu:.3f}"])

        status = "CLEAN" if new_constraints == 0 else f"+{new_constraints}c"
        print(f"[{lbl}] seed {seed}: {status} (total={total_constraints}c, "
              f"{consecutive_clean}/{CLEAN_THRESHOLD} clean)")
        seed += 1

    if consecutive_clean >= CLEAN_THRESHOLD:
        return finish("converged")
    if total_constraints > MAX_CONSTRAINTS:
        return finish("max_constraints")
    return finish("max_seeds")


# === Arm runner ===

def run_arm(args):
    N, rate, prune = args
    t0 = time.time()
    lbl = label(N, rate, prune)
    print(f"\n{'='*60}\n[{lbl}] start (pruning={'ON' if prune else 'OFF'})\n{'='*60}")
    try:
        setup_scenario(N, rate, prune)
        res = run_training(N, rate, prune)
    except Exception as e:
        print(f"[{lbl}] FATAL: {e}")
        import traceback
        traceback.print_exc()
        res = {"N": N, "rate": rate, "prune": prune, "final_constraints": -1,
               "total_seeds": -1, "training_seeds": -1, "status": "fatal", "cpu": 0}
    res["elapsed"] = round(time.time() - t0, 1)
    return res


# === Summary ===

def write_summary(results):
    os.makedirs(WORK_DIR, exist_ok=True)
    path = os.path.join(WORK_DIR, "summary.csv")
    header = ["N", "rate", "prune", "final_constraints", "total_seeds",
              "training_seeds", "status", "cpu", "elapsed"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in sorted(results, key=lambda x: (x["N"], x["rate"], -x["prune"])):
            w.writerow([r[k] for k in header])
    print(f"\nSummary written to {path}")

    by_cell = {}
    for r in results:
        by_cell.setdefault((r["N"], r["rate"]), {})[r["prune"]] = r
    print(f"\n{'cell':<12}{'on: c/seeds (status)':<30}{'off: c/seeds (status)':<30}{'Δc':>8}")
    for (N, rate) in sorted(by_cell):
        on, off = by_cell[(N, rate)].get(1), by_cell[(N, rate)].get(0)
        fmt = lambda r: f"{r['final_constraints']}/{r['total_seeds']} ({r['status']})" if r else "-"
        dc = ""
        if on and off and on["final_constraints"] > 0:
            dc = f"{(off['final_constraints']-on['final_constraints'])/on['final_constraints']*100:+.0f}%"
        print(f"N{N:02d}/{rate_label(rate):<7}{fmt(on):<30}{fmt(off):<30}{dc:>8}")


# === Main ===

def parse_cell(spec):
    n_str, r_str = spec.split(":")
    return int(n_str[1:]), float(r_str[1:])


def main():
    parser = argparse.ArgumentParser(description="Pruning ablation (corridor)")
    parser.add_argument("--cells", nargs="+", metavar="N##:r#.##",
                        help="Cells (default: %s)" %
                             " ".join(f"N{n:02d}:{rate_label(r)}" for n, r in DEFAULT_CELLS))
    parser.add_argument("--jobs", "-j", type=int, default=1)
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cells = [parse_cell(c) for c in args.cells] if args.cells else DEFAULT_CELLS
    arms = [(N, rate, p) for (N, rate) in cells for p in PRUNE_MODES]

    print(f"Pruning ablation: {len(cells)} cells x {len(PRUNE_MODES)} modes "
          f"= {len(arms)} arms, {args.jobs} workers")
    print(f"  Cells: {[f'N{n:02d}/{rate_label(r)}' for n, r in cells]}")
    print(f"  Caps: {CLEAN_THRESHOLD} clean / {MAX_SEEDS} seeds / {MAX_CONSTRAINTS} constraints")
    print(f"  Exe:  {EXE_PATH}")

    if args.dry_run:
        for a in arms:
            print(f"  {label(*a)}")
        return

    if not os.path.exists(EXE_PATH):
        print(f"ERROR: DesRail.exe not found at {EXE_PATH} (rebuild with PRUNING option)")
        sys.exit(1)

    if args.setup_only:
        for N, rate, p in arms:
            setup_scenario(N, rate, p)
            print(f"  set up {label(N, rate, p)}")
        return

    t0 = time.time()
    results = []
    if args.jobs <= 1:
        for a in arms:
            results.append(run_arm(a))
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(run_arm, a): a for a in arms}
            for fut in as_completed(futures):
                results.append(fut.result())

    write_summary(results)
    print(f"\nAblation complete in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
