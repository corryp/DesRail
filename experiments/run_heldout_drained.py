#!/usr/bin/env python3
"""Held-out eval, DRAINED, 10k seeds/cell, sliced into 10 x 1000 for HPC.

Fair re-run of the held-out test using the drain (start_warmdown): a seed's
unprotected (before) arm is classified deadlock-free ONLY if the [0,192] cohort
also empties out during the drain, closing the near-horizon detection blind spot
(see COREACH_R200_DIAGNOSIS.md / project_drain_warmdown).

Each (mode, N, rate) cell = 10 slices of 1000 seeds. A slice is one unit of work
(one PBS job); it has its own isolated work dir and its own part raw file, so
slices run fully in parallel with no contention and resume independently. After
all slices land, `--merge` concatenates them and emits the per-cell safe-seed
list that Phase C (coreach) consumes.

Outputs live under results/heldout_drained/ and work/heldout_drained/ so the
paper-cited non-drained results/heldout_eval/ are never touched.

Usage:
  # one slice (the PBS unit)
  python3 run_heldout_drained.py cell --N 2 --rate 2.00 --mode native --slice 0
  # merge a cell's slices + write safe list
  python3 run_heldout_drained.py merge --N 2 --rate 2.00 --mode native
  # local prove: first `--slices` slices for N02+N05, both modes, pooled
  python3 run_heldout_drained.py prove --slices 2 --jobs 8
"""
import argparse, os, sys, csv, shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import run_heldout_eval as H
from run_experiments import (set_csv_option, set_spawn_rate, run_exe,
                             rate_label, scenario_label, append_csv_row, load_csv_log)

# --- drain protocol constants ---
BASE_SIM_LEN = 192.0      # original horizon == warmdown point (stats window end)
DRAIN_HORIZON = 1000.0    # big-M: run past warmdown until clear/deadlock
SEEDS_PER_CELL = 10000
SLICE = 1000              # -> 10 slices per cell
N_SLICES = SEEDS_PER_CELL // SLICE

DRAINED_WORK = os.path.join(SCRIPT_DIR, "work", "heldout_drained")
DRAINED_RESULTS = os.path.join(SCRIPT_DIR, "results", "heldout_drained")
SAFE_DIR = os.path.join(DRAINED_RESULTS, "safe_seeds")

RAW_HEADER = ["seed",
              "after_deadlock", "after_dl_time", "after_throughput", "after_drained_clear",
              "before_deadlock", "before_dl_time", "before_throughput", "before_drained_clear"]


def set_or_add(path, opt, val):
    rows = list(csv.reader(open(path, newline="")))
    for r in rows:
        if r and r[0] == opt:
            r[1] = str(val)
            break
    else:
        rows.append([opt, str(val), ""])
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def slice_work_dir(N, rate, mode, sidx):
    return os.path.join(DRAINED_WORK, mode, f"N{N:02d}", rate_label(rate), f"slice_{sidx:02d}")


def cell_results_dir(N, rate, mode):
    return os.path.join(DRAINED_RESULTS, mode, f"N{N:02d}", rate_label(rate))


def part_path(N, rate, mode, sidx):
    return os.path.join(cell_results_dir(N, rate, mode), f"heldout_raw.part_{sidx:02d}.csv")


def merged_path(N, rate, mode):
    return os.path.join(cell_results_dir(N, rate, mode), "heldout_raw.csv")


def setup_slice(N, rate, mode, sidx):
    """Isolated work dir with drain enabled."""
    wdir = slice_work_dir(N, rate, mode, sidx)
    input_dir = os.path.join(wdir, "input")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(os.path.join(wdir, "output"), exist_ok=True)
    base_input = os.path.join(H.CONFIGS_DIR, f"N{N:02d}", "input")
    for fn in os.listdir(base_input):
        src = os.path.join(base_input, fn)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(input_dir, fn))
    set_spawn_rate(os.path.join(input_dir, "open_loop_spawners.csv"), rate)

    if mode == "engineered":
        # No learned set: the engineered baseline is the passing-loop constraints,
        # generated in-exe from passing_loops.csv when pl_constraints=1.
        frozen_name = None
    else:
        if mode == "native":
            frozen_src = H.frozen_constraints_path(N, rate)
        else:  # union: derived from the corridor cells; build if absent
            # (atomic write in build_union_constraints makes concurrent slices safe).
            frozen_src = H.union_path(N)
            if not os.path.exists(frozen_src):
                H.build_union_constraints(N)
        if not os.path.exists(frozen_src):
            raise FileNotFoundError(f"[{scenario_label(N, rate)}/{mode}] frozen set missing: {frozen_src}")
        frozen_name = "frozen_constraints.csv"
        shutil.copy2(frozen_src, os.path.join(wdir, frozen_name))

    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(dlexp, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp, "CONSTRAINT_EVAL", "tree")
    # drain: extend horizon to big-M, stop spawning + end stats at BASE_SIM_LEN
    set_csv_option(runctrl, "sim_len", DRAIN_HORIZON)
    set_or_add(runctrl, "start_warmdown", BASE_SIM_LEN)
    return wdir, frozen_name


def run_one(wdir, seed, warm, pl):
    """One drained sim. warm = WARM_START_FILE (learned set) or "" for none;
    pl = pl_constraints flag (1 = engineered passing-loop baseline)."""
    input_dir = os.path.join(wdir, "input")
    set_csv_option(os.path.join(input_dir, "runctrl.csv"), "seed", seed)
    set_csv_option(os.path.join(input_dir, "runctrl.csv"), "pl_constraints", pl)
    set_csv_option(os.path.join(input_dir, "dlexp_options.csv"), "WARM_START_FILE", warm)
    try:
        run_exe(wdir)
    except Exception as e:
        print(f"[{os.path.relpath(wdir, DRAINED_WORK)}] EXE ERROR "
              f"(warm={warm!r} pl={pl}) seed {seed}: {e}")
        return None
    row = next(csv.DictReader(open(os.path.join(wdir, "output", "dlexp_log.csv"))), None)
    if row is None:
        return None
    return (int(row["deadlock_found"]), float(row["deadlock_time"]),
            float(row["trains_per_hr"]), int(row.get("drained_clear", -1)))


def run_slice(N, rate, mode, sidx):
    """One PBS unit: 1000 seeds, drained, resumable. native runs before+after;
    union/engineered run after only (the unprotected arm is rate-only, from
    native). after arm: native/union apply the learned set (warm), engineered
    applies the passing-loop baseline (pl_constraints=1, no learned set)."""
    label = f"{scenario_label(N, rate)}/{mode}/slice{sidx:02d}"
    do_before = (mode == "native")
    wdir, frozen_name = setup_slice(N, rate, mode, sidx)
    # after-arm config per mode
    after_warm, after_pl = ("", 1) if mode == "engineered" else (frozen_name, 0)
    os.makedirs(cell_results_dir(N, rate, mode), exist_ok=True)
    raw = part_path(N, rate, mode, sidx)
    _, done = load_csv_log(raw)
    seeds = range(H.SEED_START + sidx * SLICE, H.SEED_START + (sidx + 1) * SLICE)
    todo = [s for s in seeds if s not in done]
    print(f"[{label}] {len(todo)} to run ({len(done)} done)", flush=True)
    for i, s in enumerate(todo):
        after = run_one(wdir, s, after_warm, after_pl)
        if after is None:
            continue
        a_dl, a_dt, a_tp, a_clear = after
        b_dl = b_dt = b_tp = b_clear = ""
        if do_before:
            before = run_one(wdir, s, "", 0)   # unprotected
            if before is not None:
                b_dl, b_dt, b_tp, b_clear = before[0], f"{before[1]:.2f}", f"{before[2]:.4f}", before[3]
        append_csv_row(raw, RAW_HEADER,
                       [s, a_dl, f"{a_dt:.2f}", f"{a_tp:.4f}", a_clear,
                        b_dl, b_dt, b_tp, b_clear])
        if (i + 1) % 250 == 0 or i == len(todo) - 1:
            print(f"[{label}] {i + 1}/{len(todo)} (seed {s})", flush=True)
    return (N, rate, mode, sidx)


def merge_cell(N, rate, mode):
    rows = []
    seen = set()
    for sidx in range(N_SLICES):
        p = part_path(N, rate, mode, sidx)
        if not os.path.exists(p):
            print(f"[merge] {scenario_label(N, rate)}/{mode}: MISSING part {sidx}")
            continue
        for r in csv.DictReader(open(p)):
            s = int(r["seed"])
            if s not in seen:
                seen.add(s); rows.append(r)
    rows.sort(key=lambda r: int(r["seed"]))
    out = merged_path(N, rate, mode)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RAW_HEADER); w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in RAW_HEADER})
    print(f"[merge] {scenario_label(N, rate)}/{mode}: {len(rows)} seeds -> {out}")
    return rows


def write_safe_list(N, rate):
    """Truly-safe seeds = unprotected arm deadlock-free AND drained-clear.
    Sourced from the native cell's before columns."""
    rows = merge_cell(N, rate, "native")
    os.makedirs(SAFE_DIR, exist_ok=True)
    safe = [int(r["seed"]) for r in rows
            if r.get("before_deadlock") == "0" and r.get("before_drained_clear") == "1"]
    anomalies = [int(r["seed"]) for r in rows
                 if r.get("before_deadlock") == "0" and r.get("before_drained_clear") == "0"]
    p = os.path.join(SAFE_DIR, f"N{N:02d}_{rate_label(rate)}.txt")
    with open(p, "w") as f:
        f.write("\n".join(map(str, safe)) + ("\n" if safe else ""))
    print(f"[safe] {scenario_label(N, rate)}: {len(safe)} safe, {len(anomalies)} anomalies -> {p}")
    if anomalies:
        print(f"[safe] {scenario_label(N, rate)} ANOMALIES (dl=0,clear=0): {anomalies[:20]}")
    return safe, anomalies


def prove(slices, jobs):
    tasks = []
    for N in (2, 5):
        for rate in (1.00, 2.00):
            for mode in ("native", "union", "engineered"):
                for sidx in range(slices):
                    tasks.append((N, rate, mode, sidx))
    print(f"PROVE: {len(tasks)} slice-jobs, {jobs} workers")
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(run_slice, *t): t for t in tasks}
        for fut in as_completed(futs):
            fut.result()
    # merge + safe lists (only the slices we ran; merge tolerates missing parts)
    for N in (2, 5):
        for rate in (1.00, 2.00):
            for mode in ("native", "union", "engineered"):
                merge_cell(N, rate, mode)
            write_safe_list(N, rate)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cell"); c.add_argument("--N", type=int, required=True)
    c.add_argument("--rate", type=float, required=True)
    c.add_argument("--mode", choices=["native", "union", "engineered"], required=True)
    c.add_argument("--slice", type=int, required=True)
    m = sub.add_parser("merge"); m.add_argument("--N", type=int, required=True)
    m.add_argument("--rate", type=float, required=True)
    m.add_argument("--mode", choices=["native", "union", "engineered", "safe"], required=True)
    pr = sub.add_parser("prove"); pr.add_argument("--slices", type=int, default=2)
    pr.add_argument("--jobs", type=int, default=8)
    args = ap.parse_args()

    if args.cmd == "cell":
        run_slice(args.N, args.rate, args.mode, args.slice)
    elif args.cmd == "merge":
        if args.mode == "safe":
            write_safe_list(args.N, args.rate)
        else:
            merge_cell(args.N, args.rate, args.mode)
    elif args.cmd == "prove":
        prove(args.slices, args.jobs)


if __name__ == "__main__":
    main()
