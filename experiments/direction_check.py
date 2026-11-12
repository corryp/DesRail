#!/usr/bin/env python3
"""Per-direction delay under each supervisor, for the asymmetry note.

The corridor is nominally symmetric, yet one direction waits longer. This
measures the effect under both supervisors so the paper can attribute it, rather
than describe it and leave the reader to guess the cause. A single replication
cannot answer it: the two directions differ in opposite senses, one in the body
of the distribution and the other in the tail, so whichever a single seed shows
is the one it happened to land on.

Direction comes from the origin-destination pair, exactly as the 1.m2 breakdown
derives it, since a pair names the direction on a corridor.

    python3 direction_check.py --N 10 --rate 3.00 --mode native --seeds 200
"""
import os, sys, csv, json, argparse, statistics, collections

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import run_heldout_eval as H                       # noqa: E402
import run_heldout_drained as D                    # noqa: E402
from run_heldout_drained import _dest              # noqa: E402
from run_experiments import rate_label             # noqa: E402
from collect_train_delays import setup             # noqa: E402

OUT = os.path.join(SCRIPT_DIR, "results", "direction_check", H.stall_policy())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--N", type=int, default=10)
    ap.add_argument("--rate", type=float, default=3.00)
    ap.add_argument("--mode", required=True,
                    choices=["native", "union", "fullunion", "engineered"])
    ap.add_argument("--seeds", type=int, default=200)
    a = ap.parse_args()
    wdir, frozen_name = setup(a.N, a.rate, a.mode)
    warm, pl = ("", 1) if a.mode == "engineered" else (frozen_name, 0)
    lo, hi = D._window(wdir)
    waits = collections.defaultdict(list)
    worst_owner = collections.Counter()
    excluded = 0
    for i in range(a.seeds):
        seed = H.SEED_START + i
        res = D.run_one(wdir, seed, warm, pl)
        if res is None or res[0] != 0 or res[3] != 1:
            excluded += 1
            continue
        best = (None, -1.0)
        with open(os.path.join(wdir, "output", "train_metric_log.csv"), newline="") as f:
            for r in csv.DictReader(f):
                try:
                    done = float(r.get("completion_time") or 0)
                    w = float(r.get("total_wait_time") or 0)
                except ValueError:
                    continue
                if done < lo or (hi > 0 and done > hi):
                    continue
                k = f"{(r.get('start_loc') or '?')}>{_dest(r.get('job_list'))}"
                if "?" in k:
                    continue
                waits[k].append(w)
                if w > best[1]:
                    best = (k, w)
        if best[0]:
            worst_owner[best[0]] += 1
        if (i + 1) % 50 == 0:
            print(f"[{a.mode}] {i + 1}/{a.seeds}", flush=True)
    os.makedirs(OUT, exist_ok=True)
    summary = {"mode": a.mode, "N": a.N, "rate": a.rate,
               "replications": a.seeds - excluded, "excluded": excluded,
               "worst_owner": dict(worst_owner), "by_direction": {}}
    for k, v in sorted(waits.items()):
        v.sort()
        summary["by_direction"][k] = {
            "n": len(v), "mean": round(statistics.mean(v), 5),
            "p50": round(v[len(v) // 2], 5), "p95": round(v[int(0.95 * len(v))], 5),
            "max": round(v[-1], 5),
            "over_1h": sum(1 for x in v if x > 1.0),
            "over_2h": sum(1 for x in v if x > 2.0)}
    p = os.path.join(OUT, f"N{a.N:02d}_r{a.rate:.2f}_{a.mode}.json")
    with open(p, "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=1))
    print(f"wrote {p}")


if __name__ == "__main__":
    main()
