#!/usr/bin/env python3
"""Per-train delay values for a distribution figure.

The campaign kept only per-seed aggregates: train_metric_log.csv is summarised
and then overwritten, because retaining it for every replication would have been
around 230 GiB. A figure whose unit is the train therefore needs the runs done
again with the per-train rows kept, which is cheap for a handful of cells.

Everything that defines a cell is taken from the campaign's own runner, so a
replication here is the replication the campaign ran: the same configs, the same
drain to a 1,000 h horizon with the spawners stopped at 192 h, the same frozen
constraint set, and the same window filter on completion_time, without which the
drain tail is averaged in and nothing matches the campaign's figures.

It writes into work/train_delays and results/train_delays, never the campaign's
own slice directories.

    python3 collect_train_delays.py --N 10 --rate 3.00 --mode native --seeds 100
"""
import os, sys, csv, gzip, shutil, argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import run_heldout_eval as H                                    # noqa: E402
import run_heldout_drained as D                                 # noqa: E402
from run_experiments import set_csv_option, set_spawn_rate, rate_label  # noqa: E402

WORK = os.path.join(SCRIPT_DIR, "work", "train_delays", H.stall_policy())
RESULTS = os.path.join(SCRIPT_DIR, "results", "train_delays", H.stall_policy())


def setup(N, rate, mode, slice_idx=None):
    """A cell work dir, built exactly as run_heldout_drained.setup_slice builds
    one, but under this script's own root.

    slice_idx puts the cell under its own s<nn>/ subdirectory. A caller that
    runs one process per cell can leave it None; a caller that runs several
    processes on the same cell must pass it, because the work dir holds
    input/runctrl.csv and output/train_metric_log.csv and every replication
    rewrites both. Ten slices sharing one directory is what killed 93 of the
    100 jobs in the 2026-10-26 extraction: each read another slice's seed line
    and another slice's half-written log, giving "Option 'seed' not found in
    runctrl.csv" and "_csv.Error: line contains NUL". The six that survived
    were in the same race, so their output was discarded too."""
    wdir = os.path.join(WORK, mode, f"N{N:02d}", rate_label(rate))
    if slice_idx is not None:
        wdir = os.path.join(wdir, f"s{slice_idx:02d}")
    input_dir = os.path.join(wdir, "input")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(os.path.join(wdir, "output"), exist_ok=True)
    base_input = os.path.join(H.CONFIGS_DIR, f"N{N:02d}", "input")
    for fn in os.listdir(base_input):
        src = os.path.join(base_input, fn)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(input_dir, fn))
    set_spawn_rate(os.path.join(input_dir, "open_loop_spawners.csv"), rate)
    frozen_name = None
    if mode != "engineered":
        if mode == "native":
            frozen_src = H.frozen_constraints_path(N, rate)
        else:
            full = mode == "fullunion"
            frozen_src = H.union_path(N, full=full)
            if not os.path.exists(frozen_src):
                H.build_union_constraints(N, include_nonconverged=full)
        if not os.path.exists(frozen_src):
            raise FileNotFoundError(f"frozen set missing: {frozen_src}")
        frozen_name = "frozen_constraints.csv"
        shutil.copy2(frozen_src, os.path.join(wdir, frozen_name))
    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(dlexp, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp, "CONSTRAINT_EVAL", "tree")
    set_csv_option(runctrl, "sim_len", D.DRAIN_HORIZON)
    D.set_or_add(runctrl, "start_warmdown", D.BASE_SIM_LEN)
    return wdir, frozen_name


def trains(wdir):
    """Every train that completed inside the stats window, as (wait, spawn).
    A blank spawn_delay is absent, not zero: the column is a string field so a
    persistent train's later jobs can leave it empty, and reading those as 0
    would pull the spawn distribution toward the origin."""
    path = os.path.join(wdir, "output", "train_metric_log.csv")
    lo, hi = D._window(wdir)
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                done = float(r.get("completion_time", 0) or 0)
                w = float(r.get("total_wait_time", 0) or 0)
            except ValueError:
                continue
            if done < lo or (hi > 0 and done > hi):
                continue
            sd = (r.get("spawn_delay") or "").strip()
            try:
                s = float(sd) if sd else None
            except ValueError:
                s = None
            out.append((w, s))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--N", type=int, required=True)
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--mode", required=True,
                    choices=["native", "union", "fullunion", "engineered"])
    ap.add_argument("--seeds", type=int, default=100)
    ap.add_argument("--first-seed", type=int, default=H.SEED_START)
    a = ap.parse_args()

    wdir, frozen_name = setup(a.N, a.rate, a.mode)
    warm, pl = ("", 1) if a.mode == "engineered" else (frozen_name, 0)
    odir = os.path.join(RESULTS, a.mode, f"N{a.N:02d}", rate_label(a.rate))
    os.makedirs(odir, exist_ok=True)
    out = os.path.join(odir, "trains.csv.gz")
    kept = dl = 0
    with gzip.open(out, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["seed", "wait_h", "spawn_h"])
        for i in range(a.seeds):
            seed = a.first_seed + i
            res = D.run_one(wdir, seed, warm, pl)
            if res is None:
                continue
            # A deadlocked replication has no steady state, and the campaign's
            # own tables exclude it, so this does too.
            if res[0] != 0 or res[3] != 1:
                dl += 1
                continue
            for wv, sv in trains(wdir):
                w.writerow([seed, f"{wv:.6f}", "" if sv is None else f"{sv:.6f}"])
                kept += 1
            if (i + 1) % 25 == 0:
                print(f"[{a.mode} N{a.N:02d}/r{a.rate:.2f}] {i + 1}/{a.seeds} seeds, "
                      f"{kept:,} trains, {dl} excluded", flush=True)
    print(f"[{a.mode} N{a.N:02d}/r{a.rate:.2f}] done: {kept:,} trains from "
          f"{a.seeds - dl} replications ({dl} excluded) -> {out}", flush=True)


if __name__ == "__main__":
    main()
