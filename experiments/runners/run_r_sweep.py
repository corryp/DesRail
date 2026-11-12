#!/usr/bin/env python3
"""Residual deadlock against the stopping threshold R, on small corridors (N=2, 5, 10).

Plan: deadlock_paper/revision/R_SWEEP_N2_PLAN.md. Discard policy throughout, as
the paper reports.

Training. One cell is trained once, to the smallest R, and its learned set is
snapshotted; the threshold is then raised and the same cell resumed. The corridor
runner resumes from its training_log.csv and recounts the clean tail, so the run
continues the same seed sequence and each snapshot is exactly the set a fresh run
at that R would learn. The snapshots are therefore nested.

Held-out. Each snapshot is evaluated drained, with the protocol of
run_heldout_drained.py (horizon 1000 h, spawning and statistics stopping at
192 h), on seeds from 100,000 upwards. Only the protected arm is run: the
unprotected arm does not depend on R. A chunk of seeds is one unit of work with its
own work dir and part file, resumable.

Work goes under $DESRAIL_WORK/r_sweep (default <bundle>/work/r_sweep) and results
under <bundle>/results/r_sweep, so nothing of the 2026-10-22 campaign is touched.

Usage:
  python3 runners/run_r_sweep.py train   --N 2 --rate 1.50
  python3 runners/run_r_sweep.py train   --N 5 --rate 1.50 --up-to 100000
  python3 runners/run_r_sweep.py heldout --N 2 --rate 1.50 --R 500 --start 100000 --count 25000
  python3 runners/run_r_sweep.py replay  --N 2 --rate 1.50 --from-R 5000 --seeds 607795,626721
  python3 runners/run_r_sweep.py heldout --N 2 --rate 1.50 --R 5000t --start 1100000 --count 25000
  python3 runners/run_r_sweep.py summary

R = 100,000 is Algorithm 1 with a larger stopping threshold, resumed from the R =
5,000 cell, so its training seeds run past 100,000 and its held-out batch starts at
10,000,000. heldout refuses any start at or below a snapshot's last training seed.

Replay. Starting from a snapshot's set, the given seeds (normally the held-out seeds
that deadlocked against it) are run again with learning on, exactly as a training
seed is run, in passes over the list until one pass is clean. The result is a new
snapshot, R{from_R}t. Those seeds are then training seeds, so the new set is
evaluated on seeds above every earlier held-out range.
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys

BUNDLE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["STALL_POLICY"] = "discard"          # before importing the eval modules
sys.path.insert(0, BUNDLE)
import run_heldout_eval as H                     # noqa: E402
import run_heldout_drained as D                  # noqa: E402
from run_experiments import rate_label, append_csv_row, load_csv_log  # noqa: E402

N = 2                                            # set from --N in main()
R_VALUES = [100, 200, 500, 1000, 2000, 5000, 100000]
DEFAULT_UP_TO = 5000             # train stops here unless --up-to says otherwise
# Training and held-out seeds are kept apart by heldout(), which refuses to start
# at or below a snapshot's last training seed.
MAX_TRAIN_SEEDS = 5000000
SWEEP_WORK = os.path.join(os.environ.get("DESRAIL_WORK") or os.path.join(BUNDLE, "work"),
                          "r_sweep")
RESULTS = os.environ.get("R_SWEEP_RESULTS") or os.path.join(BUNDLE, "results", "r_sweep")
HELDOUT_HEADER = ["seed", "deadlock", "dl_time", "throughput", "drained_clear"]


def cell_dir(rate):
    return os.path.join(RESULTS, f"N{N:02d}", rate_label(rate))


def snapshot_dir(rate, R):
    return os.path.join(cell_dir(rate), f"R{R}")


def training_cell(rate):
    """Where run_corridor.py keeps this cell under DESRAIL_WORK=SWEEP_WORK."""
    return os.path.join(SWEEP_WORK, "corridor", "discard", f"N{N:02d}", rate_label(rate))


# === Training ===

def train(rate, up_to=DEFAULT_UP_TO):
    sdir = training_cell(rate)
    for R in [r for r in R_VALUES if r <= up_to]:
        snap = snapshot_dir(rate, R)
        if os.path.exists(os.path.join(snap, "meta.json")):
            print(f"[R={R}] snapshot exists, skipping", flush=True)
            continue
        env = dict(os.environ, CLEAN_THRESHOLD=str(R), MAX_SEEDS=str(MAX_TRAIN_SEEDS),
                   STALL_POLICY="discard", DESRAIL_WORK=SWEEP_WORK)
        cmd = [sys.executable, os.path.join(BUNDLE, "runners", "run_corridor.py"),
               "--scenarios", f"N{N:02d}:{rate_label(rate)}", "--jobs", "1",
               "--policy", "discard", "--skip-benchmark"]
        print(f"[R={R}] training: {' '.join(cmd)}", flush=True)
        subprocess.run(cmd, env=env, cwd=BUNDLE, check=True)

        rows, _ = load_csv_log(os.path.join(sdir, "training_log.csv"))
        tail = 0
        for r in reversed(rows):
            if int(r["deadlock_found"]) == 0:
                tail += 1
            else:
                break
        if tail < R:
            raise SystemExit(f"[R={R}] training stopped with {tail} clean seeds, "
                             f"not {R} (last seed {rows[-1]['seed']}); not converged")
        os.makedirs(snap, exist_ok=True)
        shutil.copy2(os.path.join(sdir, "constraints.csv"), os.path.join(snap, "constraints.csv"))
        meta = {"N": N, "rate": rate, "R": R, "last_seed": int(rows[-1]["seed"]),
                "constraints": int(rows[-1]["total_constraints"]),
                "cpu_s": round(sum(float(r["cpu_time"]) for r in rows), 3)}
        json.dump(meta, open(os.path.join(snap, "meta.json"), "w"), indent=1)
        print(f"[R={R}] snapshot: {meta}", flush=True)


# === Replay of given seeds ===

MAX_REPLAY_PASSES = 20


def deadlocked_heldout_seeds(rate, R):
    seeds = set()
    snap = snapshot_dir(rate, R)
    for f in os.listdir(snap):
        if f.startswith("heldout.part_"):
            for r in csv.DictReader(open(os.path.join(snap, f))):
                if int(r["deadlock"]) or int(r["drained_clear"]) == 0:
                    seeds.add(int(r["seed"]))
    return sorted(seeds)


def replay(rate, from_R, seeds):
    src = os.path.join(snapshot_dir(rate, from_R), "constraints.csv")
    if not os.path.exists(src):
        raise SystemExit(f"no snapshot {src}")
    if seeds == "auto":
        seeds = deadlocked_heldout_seeds(rate, from_R)
        print(f"[replay] deadlocked held-out seeds of R{from_R}: {seeds}", flush=True)
    snap = snapshot_dir(rate, f"{from_R}t")
    if os.path.exists(os.path.join(snap, "meta.json")):
        print(f"[replay] {snap} exists, skipping", flush=True)
        return
    # The training runner's own setup and per-seed options, in a work tree of our own.
    os.environ["DESRAIL_WORK"] = os.path.join(SWEEP_WORK, "replay", f"R{from_R}t")
    sys.path.insert(0, os.path.join(BUNDLE, "runners"))
    import run_corridor as RC
    from run_experiments import parse_dlexp_log, run_exe, set_csv_option, set_or_add_csv_option
    sdir = RC.setup_scenario(N, rate)
    inp, out = os.path.join(sdir, "input"), os.path.join(sdir, "output")
    runctrl, dlexp = os.path.join(inp, "runctrl.csv"), os.path.join(inp, "dlexp_options.csv")
    set_csv_option(dlexp, "MAX_ITERATIONS", RC.MAX_ITERATIONS_TRAINING)
    set_csv_option(dlexp, "CONSTRAINT_EVAL", "tree")
    set_csv_option(runctrl, "pl_constraints", 0)
    set_or_add_csv_option(dlexp, "SCC_LOG", 1)
    set_csv_option(dlexp, "WARM_START_FILE", "warm_constraints.csv")
    warm = os.path.join(sdir, "warm_constraints.csv")
    shutil.copy2(src, warm)
    os.makedirs(os.path.join(snap, "scc_logs"), exist_ok=True)
    log = os.path.join(snap, "replay_log.csv")
    total = None
    for p in range(1, MAX_REPLAY_PASSES + 1):
        dirty = 0
        for s in seeds:
            set_csv_option(runctrl, "seed", s)
            run_exe(sdir)
            results = parse_dlexp_log(os.path.join(out, "dlexp_log.csv"))
            if not results:
                raise SystemExit(f"[replay] empty dlexp_log at seed {s}")
            new = sum(r["new_constraints"] for r in results)
            # Same classification and discard rule as run_corridor.run_training_scenario.
            resolved = len(results) < RC.MAX_ITERATIONS_TRAINING \
                and not results[-1]["deadlock_found"]
            dl = int(any(r["deadlock_found"] or r.get("stalled", 0)
                         or r.get("run_incomplete", 0) for r in results))
            kept = int(new > 0 and resolved)
            if kept:
                shutil.copy2(os.path.join(out, "generated_constraints.csv"), warm)
                total = results[-1]["total_constraints"]
                scc = os.path.join(out, "scc_log.csv")
                if os.path.exists(scc):
                    shutil.copy2(scc, os.path.join(snap, "scc_logs", f"pass{p}_seed{s}.csv"))
            dirty += dl
            append_csv_row(log, ["pass", "seed", "deadlock_found", "new_constraints",
                                 "kept", "iterations", "total_constraints"],
                           [p, s, dl, new, kept, len(results), total if total is not None else ""])
            print(f"[replay p{p}] seed {s}: deadlock={dl} +{new}c kept={kept} "
                  f"iterations={len(results)}", flush=True)
        if dirty == 0:
            break
    else:
        raise SystemExit(f"[replay] still deadlocking after {MAX_REPLAY_PASSES} passes")
    shutil.copy2(warm, os.path.join(snap, "constraints.csv"))
    meta = json.load(open(os.path.join(snapshot_dir(rate, from_R), "meta.json")))
    meta.update({"R": f"{from_R}t", "replayed_seeds": seeds, "passes": p,
                 "constraints": total if total is not None else meta["constraints"]})
    json.dump(meta, open(os.path.join(snap, "meta.json"), "w"), indent=1)
    print(f"[replay] snapshot: {meta}", flush=True)


# === Held-out ===

def heldout(rate, R, start, count):
    snap = os.path.join(snapshot_dir(rate, R), "constraints.csv")
    if not os.path.exists(snap):
        raise SystemExit(f"no snapshot {snap}; run train first")
    last = json.load(open(os.path.join(snapshot_dir(rate, R), "meta.json")))["last_seed"]
    if start <= last:
        raise SystemExit(f"held-out start {start} is not above the last training seed {last}")
    # Point the drained runner's setup at this snapshot and at a work dir of our own.
    H.frozen_constraints_path = lambda _N, _rate: snap
    D.DRAINED_WORK = os.path.join(SWEEP_WORK, "heldout", f"R{R}", f"s{start}")
    wdir, frozen_name = D.setup_slice(N, rate, "native", 0)
    part = os.path.join(snapshot_dir(rate, R), f"heldout.part_{start}_{count}.csv")
    _, done = load_csv_log(part)
    todo = [s for s in range(start, start + count) if s not in done]
    print(f"[R={R} {rate_label(rate)}] seeds {start}..{start + count - 1}: "
          f"{len(todo)} to run ({len(done)} done)", flush=True)
    for i, s in enumerate(todo):
        out = D.run_one(wdir, s, frozen_name, 0)
        if out is None:
            continue
        dl, dt, tp, clear, _delay = out
        append_csv_row(part, HELDOUT_HEADER, [s, dl, f"{dt:.2f}", f"{tp:.4f}", clear])
        if (i + 1) % 1000 == 0 or i == len(todo) - 1:
            print(f"[R={R} {rate_label(rate)}] {i + 1}/{len(todo)} (seed {s})", flush=True)


# === Summary and checks ===

def read_set(path):
    groups = {}
    for r in list(csv.reader(open(path, newline="")))[1:]:
        if r:
            groups.setdefault(r[0], []).append(tuple(r[1:]))
    return {tuple(sorted(g)) for g in groups.values()}


def summary():
    global N
    out_rows = []
    for n_dir in sorted(d for d in os.listdir(RESULTS) if d.startswith("N")):
        N = int(n_dir[1:])
        out_rows += summary_n()
    path = os.path.join(RESULTS, "summary.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["N", "rate", "R", "training_seeds", "constraints", "nested_in_prev",
                    "heldout_n", "deadlocked", "ci95_lo", "ci95_hi",
                    "new_deadlocks_vs_prev_R"])
        w.writerows(out_rows)
    for r in out_rows:
        print(r)
    print(f"-> {path}")


def summary_n():
    out_rows = []
    for rate_dir in sorted(os.listdir(os.path.join(RESULTS, f"N{N:02d}"))):
        rate = float(rate_dir[1:])
        # A replay snapshot (5000t) is compared with its base; a plain R with the
        # previous plain R, since training resumes from the base and not the replay.
        prev_set, prev_dl, base = None, None, None
        for R in [x for r in R_VALUES for x in (r, f"{r}t")]:
            snap = snapshot_dir(rate, R)
            if not os.path.exists(os.path.join(snap, "meta.json")):
                continue
            meta = json.load(open(os.path.join(snap, "meta.json")))
            cset = read_set(os.path.join(snap, "constraints.csv"))
            nested = "" if prev_set is None else int(prev_set <= cset)
            res = {}
            for f in os.listdir(snap):
                if f.startswith("heldout.part_"):
                    for r in csv.DictReader(open(os.path.join(snap, f))):
                        res[int(r["seed"])] = (int(r["deadlock"]), int(r["drained_clear"]))
            n = len(res)
            dl_seeds = {s for s, (dl, clear) in res.items() if dl or clear == 0}
            k = len(dl_seeds)
            lo, hi = H.clopper_pearson(k, n)
            # Theorem 2 check: on seeds common to both, deadlocked seeds at this R
            # must be a subset of those at the previous R.
            violations = ""
            if prev_dl is not None:
                common = set(res) & prev_dl[1]
                violations = len({s for s in dl_seeds & common if s not in prev_dl[0]})
            out_rows.append([N, rate_label(rate), R, meta["last_seed"], meta["constraints"],
                             nested, n, k, f"{lo:.2e}", f"{hi:.2e}", violations])
            if isinstance(R, int):
                base = (cset, (dl_seeds, set(res)))
                prev_set, prev_dl = base
            else:
                prev_set, prev_dl = base     # the next plain R compares with the base
    return out_rows


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train"); t.add_argument("--rate", type=float, required=True)
    t.add_argument("--N", type=int, default=2)
    t.add_argument("--up-to", type=int, default=DEFAULT_UP_TO,
                   help="largest R to train to; 100000 resumes from the R=5000 cell")
    h = sub.add_parser("heldout"); h.add_argument("--rate", type=float, required=True)
    h.add_argument("--N", type=int, default=2)
    h.add_argument("--R", required=True)  # an R value, or a replay snapshot such as 5000t
    h.add_argument("--start", type=int, required=True)
    h.add_argument("--count", type=int, required=True)
    rp = sub.add_parser("replay"); rp.add_argument("--rate", type=float, required=True)
    rp.add_argument("--N", type=int, default=2)
    rp.add_argument("--from-R", type=int, required=True)
    rp.add_argument("--seeds", required=True,
                    help="comma-separated, or 'auto': the snapshot's deadlocked held-out seeds")
    sub.add_parser("summary")
    a = p.parse_args()
    global N
    N = getattr(a, "N", N)
    if a.cmd == "train":
        train(a.rate, a.up_to)
    elif a.cmd == "replay":
        replay(a.rate, a.from_R, "auto" if a.seeds == "auto"
               else [int(x) for x in a.seeds.split(",")])
    elif a.cmd == "heldout":
        heldout(a.rate, a.R, a.start, a.count)
    else:
        summary()


if __name__ == "__main__":
    main()
