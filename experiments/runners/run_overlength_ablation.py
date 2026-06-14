#!/usr/bin/env python3
"""
Overlength pruning ablation driver (2026-06-04).

Sister of run_pruning_ablation.py, but on the OVERLENGTH corridor instead of the
plain corridor. Motivation: the plain-corridor ablation showed pruning's marginal
effect is now ~0% under the reason-based detection redesign (commit 439608f); the
hypothesis is that constraint *stacking* (PL + learned overlength constraints
co-occupying the same arcs) is far more prominent in the overlength regime, so
more removable bystanders may survive into the root-cause SCC and pruning may bite.

For each cell (N, fraction) it runs TRAINING twice under the SAME exe / seeds /
network: pruning ON (PRUNING=1, the published method) and OFF (PRUNING=0). Only
the toggle differs, so any gap in the final learned-constraint count is pruning's
contribution. No benchmark phases — the ablation only needs the converged counts.

Overlength-specific settings (differ from the plain-corridor ablation):
  * configs_overlength/N{XX} base config (mixed overlength fleet)
  * pl_constraints = 1   (PL active; learner adds the residual overlength
                          constraints -> this is the stacking regime)
  * CONSTRAINT_EVAL = flat   (tree-eval bug on same-segment overlength
                              constraints; flat is the campaign default here)
  * spawners written by write_overlength_spawners at lambda=2.00, given fraction

Termination per arm (campaign-standard): 100 consecutive clean seeds, OR
seeds > 3000, OR total constraints > 25000. The no-prune arm is expected to reach
a higher count (or bail at 25k) if stacking leaves prunable bystanders.

Folder structure (under work/overlength_ablation/):
    N{XX}/f{X.XX}/p{0,1}/
        input/ output/ training_log.csv constraints.csv warm_constraints.csv
    summary.csv     one row per (N, fraction, pruning) + on/off Delta table

Usage:
    python run_overlength_ablation.py                     # default meaty cells
    python run_overlength_ablation.py --jobs 4
    python run_overlength_ablation.py --cells N15:f0.10 N20:f0.10
    python run_overlength_ablation.py --setup-only
    python run_overlength_ablation.py --dry-run

Requires a DesRail.exe built with the PRUNING option.
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))      # .../runners
CAMPAIGN_DIR = os.path.dirname(SCRIPT_DIR)                   # the set folder
SET_LABEL = "overlength_ablation"
WORK_DIR = os.path.join(CAMPAIGN_DIR, "work", SET_LABEL)

# Overlength helpers (csv/exe/spawner) + config bootstrap.
sys.path.insert(0, CAMPAIGN_DIR)
from run_overlength_exp import (  # noqa: E402
    set_csv_option,
    parse_dlexp_log,
    load_csv_log,
    run_exe,
    append_csv_row,
    read_base_spawners,
    write_overlength_spawners,
)
sys.path.insert(0, SCRIPT_DIR)
from run_overlength import (  # noqa: E402
    ensure_base_config,
    base_config_dir,
    EXE_PATH,
)

# === Parameters ===

SPAWN_RATE = 2.00
CLEAN_THRESHOLD = 100
MAX_SEEDS = 3000
MAX_CONSTRAINTS = 25000
MAX_ITERATIONS_TRAINING = 2000

# Meaty cells: the highest-count overlength scenarios (most stacking), where
# pruning is most likely to bite, plus one mid-size control (N10/f0.25, 4687c).
# Pruned-arm reference counts: N15/f0.10=17177, N20/f0.10=15288,
# N20/f0.25=10445, N10/f0.25=4687.
DEFAULT_CELLS = [(15, 0.10), (20, 0.10), (20, 0.25), (10, 0.25)]
PRUNE_MODES = [1, 0]  # 1 = pruning on (published), 0 = ablated


def label(N, fraction, prune):
    return f"N{N:02d}/f{fraction:.2f}/p{prune}"


def scenario_dir(N, fraction, prune):
    return os.path.join(WORK_DIR, f"N{N:02d}", f"f{fraction:.2f}", f"p{prune}")


def set_or_add_option(filepath, option_name, value):
    """Like set_csv_option but appends the row if missing (configs may lack
    the PRUNING option)."""
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


# === Setup ===

def setup_scenario(N, fraction, prune):
    """Populate an overlength scenario dir, set lambda/fraction spawners,
    pl_constraints=1, flat eval, MAX_ITERATIONS, and the PRUNING toggle."""
    ensure_base_config(N)
    sdir = scenario_dir(N, fraction, prune)
    input_dir = os.path.join(sdir, "input")
    output_dir = os.path.join(sdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    base_input = os.path.join(base_config_dir(N), "input")
    for fname in os.listdir(base_input):
        src = os.path.join(base_input, fname)
        dst = os.path.join(input_dir, fname)
        if os.path.isfile(src):
            shutil.copy2(src, dst)

    # PL-only learning: engineered spawn-hold constraints must not be loaded.
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)

    # Mixed overlength fleet at lambda=2.00, given fraction.
    base_spawners = read_base_spawners(base_config_dir(N))
    write_overlength_spawners(
        os.path.join(input_dir, "open_loop_spawners.csv"),
        SPAWN_RATE, fraction, base_spawners)

    dlexp_opts = os.path.join(input_dir, "dlexp_options.csv")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(dlexp_opts, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    set_csv_option(dlexp_opts, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp_opts, "WARM_START_FILE", "")
    set_csv_option(runctrl, "pl_constraints", 1)
    set_or_add_option(dlexp_opts, "PRUNING", prune)

    # Fresh start each arm — no warm-start carryover between modes.
    warm_file = os.path.join(sdir, "warm_constraints.csv")
    if os.path.exists(warm_file):
        os.remove(warm_file)
    return sdir


# === Training (one arm) ===

def run_training(N, fraction, prune):
    lbl = label(N, fraction, prune)
    sdir = scenario_dir(N, fraction, prune)
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
        return {"N": N, "fraction": fraction, "prune": prune,
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
        set_csv_option(dlexp_opts, "WARM_START_FILE",
                       "warm_constraints.csv" if os.path.exists(warm_file) else "")
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
    N, fraction, prune = args
    t0 = time.time()
    lbl = label(N, fraction, prune)
    print(f"\n{'='*60}\n[{lbl}] start (pruning={'ON' if prune else 'OFF'})\n{'='*60}")
    try:
        setup_scenario(N, fraction, prune)
        res = run_training(N, fraction, prune)
    except Exception as e:
        print(f"[{lbl}] FATAL: {e}")
        import traceback
        traceback.print_exc()
        res = {"N": N, "fraction": fraction, "prune": prune,
               "final_constraints": -1, "total_seeds": -1,
               "training_seeds": -1, "status": "fatal", "cpu": 0}
    res["elapsed"] = round(time.time() - t0, 1)
    return res


# === Summary ===

def write_summary(results):
    os.makedirs(WORK_DIR, exist_ok=True)
    path = os.path.join(WORK_DIR, "summary.csv")
    header = ["N", "fraction", "prune", "final_constraints", "total_seeds",
              "training_seeds", "status", "cpu", "elapsed"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in sorted(results, key=lambda x: (x["N"], x["fraction"], -x["prune"])):
            w.writerow([r[k] for k in header])
    print(f"\nSummary written to {path}")

    by_cell = {}
    for r in results:
        by_cell.setdefault((r["N"], r["fraction"]), {})[r["prune"]] = r
    print(f"\n{'cell':<14}{'on: c/seeds (status)':<30}"
          f"{'off: c/seeds (status)':<30}{'Δc':>8}")
    for (N, fraction) in sorted(by_cell):
        on, off = by_cell[(N, fraction)].get(1), by_cell[(N, fraction)].get(0)
        fmt = lambda r: f"{r['final_constraints']}/{r['total_seeds']} ({r['status']})" if r else "-"
        dc = ""
        if on and off and on["final_constraints"] > 0:
            dc = f"{(off['final_constraints']-on['final_constraints'])/on['final_constraints']*100:+.0f}%"
        print(f"N{N:02d}/f{fraction:.2f}  {fmt(on):<30}{fmt(off):<30}{dc:>8}")


# === Main ===

def parse_cell(spec):
    n_str, f_str = spec.split(":")
    return int(n_str.lstrip("N")), float(f_str.lstrip("f"))


def main():
    parser = argparse.ArgumentParser(description="Pruning ablation (overlength)")
    parser.add_argument("--cells", nargs="+", metavar="N##:f#.##",
                        help="Cells (default: %s)" %
                             " ".join(f"N{n:02d}:f{f:.2f}" for n, f in DEFAULT_CELLS))
    parser.add_argument("--jobs", "-j", type=int, default=1)
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cells = [parse_cell(c) for c in args.cells] if args.cells else DEFAULT_CELLS
    arms = [(N, fraction, p) for (N, fraction) in cells for p in PRUNE_MODES]

    print(f"Overlength pruning ablation: {len(cells)} cells x {len(PRUNE_MODES)} "
          f"modes = {len(arms)} arms, {args.jobs} workers")
    print(f"  Cells: {[f'N{n:02d}/f{f:.2f}' for n, f in cells]}")
    print(f"  Caps: {CLEAN_THRESHOLD} clean / {MAX_SEEDS} seeds / "
          f"{MAX_CONSTRAINTS} constraints | lambda={SPAWN_RATE} | flat eval")
    print(f"  Exe:  {EXE_PATH}")

    if args.dry_run:
        for a in arms:
            print(f"  {label(*a)}")
        return

    if not os.path.exists(EXE_PATH):
        print(f"ERROR: DesRail.exe not found at {EXE_PATH} (rebuild with PRUNING)")
        sys.exit(1)

    if args.setup_only:
        for N, fraction, p in arms:
            setup_scenario(N, fraction, p)
            print(f"  set up {label(N, fraction, p)}")
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
