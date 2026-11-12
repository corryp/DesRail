#!/usr/bin/env python3
"""Per-train delay distributions over the full 10,000 held-out replications.

collect_train_delays.py writes one row per train, which is fine for a hundred
replications and is 50 million rows across ten cells at ten thousand. This keeps
what a density, a tail analysis and a points overlay actually need, and nothing
else:

  * one summary row per replication: counts, mean, percentiles, max;
  * a histogram on log10(minutes) in 0.01 dex bins, which reproduces the kernel
    density to far finer resolution than any figure resolves;
  * every train above TAIL_MIN kept individually, with its seed, because the
    tail is the part of this distribution the reviewer asked about and the part
    a histogram bins away;
  * a fixed-size reservoir sample of raw values for drawing points.

Trains below NEGLIGIBLE_MIN are counted, not binned: the shortest wait recorded
is 7 ms, and admitting those to a log axis stretches it three decades below
anything legible.

It also carries the over-conservatism metric of comment 6.4, whose direction was
set on 2026-10-15: the number of trains on the network. Per replication it
records the time-average trains on the network by an exact interval sweep, the
mean time a train spends on the network, and the five components of that time,
plus an hourly occupancy profile so the metric can be shown over time rather
than as one number.

The two averages are Little's law L and W, but they are stored and reported as
n_on_network and time_on_network. L is train length throughout the paper and W
is a working set in Algorithm 1, so the conventional symbols would clash with
notation the reader has already been given.

Two properties of train_metric_log.csv this relies on, both verified on
N10/r3.00 rather than assumed, because the decision queue recorded one of each:

  * a train is in the system from ready_time to completion_time, since
    completion_time - ready_time == total_time to 1e-6. The spawn hold happens
    BEFORE ready_time and is disjoint from time in system, so entry is
    ready_time and not ready_time + spawn_delay;
  * total_travel_time CONTAINS total_wait_time, so the decomposition that adds
    up is dwell + accel + coast + decel + wait, again exact to 1e-6. Adding
    dwell + travel + wait instead double-counts every stopped minute.

Occupancy uses every train clipped to the window, not only those completing
inside it: a train finishing at 200 h still occupies the corridor at 190.

A replication that deadlocks or fails to drain is excluded and counted, matching
the campaign's own tables.

    python3 extract_train_delays.py slice --N 10 --rate 3.00 --mode native --slice 0
    python3 extract_train_delays.py merge --N 10 --rate 3.00 --mode native
"""
import os, sys, csv, gzip, math, json, random, argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import run_heldout_eval as H                       # noqa: E402
import run_heldout_drained as D                    # noqa: E402
from run_experiments import rate_label             # noqa: E402
from collect_train_delays import setup, trains     # noqa: E402

SLICE = 1000
N_SLICES = 10
NEGLIGIBLE_MIN = 0.1          # six seconds
TAIL_MIN = 120.0              # two hours; every train above this is kept whole
BIN_LO, BIN_HI, BIN_W = -1.0, 3.6, 0.01
N_BINS = int(round((BIN_HI - BIN_LO) / BIN_W))
RESERVOIR = 4000
PCTS = (50, 90, 95, 99)
PROFILE_HI = 212             # hours; one bin per hour, past the latest completion
COMPONENTS = ("dwell", "accel", "coast", "decel", "wait")
# Levels the occupancy-time histogram carries. Anything at or above the last one
# is counted in it, so the array never silently drops time; the corridor's
# busiest measured hour averages about 15 trains, so 128 is far above anything
# reachable and the overflow bin should stay empty.
OCC_LEVELS = 128
# The per-replication row layout, defined here so run_slice and merge cannot
# disagree: merge used to copy the names out of part_00, which meant a rename
# here silently left the merged cells labelled the old way.
PER_SEED_COLS = (["seed", "n_trains", "n_negligible", "mean_wait"]
                 + [f"p{q}_wait" for q in PCTS]
                 + ["max_wait", "mean_spawn", "max_spawn",
                    "n_on_network", "peak_on_network", "n_completed",
                    "time_on_network"]
                 + [f"sum_{c}" for c in COMPONENTS])
RESULTS = os.path.join(SCRIPT_DIR, "results", "train_delays_full", H.stall_policy())


def _bin(v):
    i = int((math.log10(v) - BIN_LO) / BIN_W)
    return min(N_BINS - 1, max(0, i))


def _pct(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    k = min(len(sorted_vals) - 1, max(0, int(round(q / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def read_log(wdir):
    """Every train in the run, with the fields the metrics need."""
    path = os.path.join(wdir, "output", "train_metric_log.csv")
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            def g(k):
                try:
                    return float(r.get(k) or 0)
                except ValueError:
                    return 0.0
            sd = (r.get("spawn_delay") or "").strip()
            try:
                spawn = float(sd) if sd else None
            except ValueError:
                spawn = None
            out.append(dict(ready=g("ready_time"), done=g("completion_time"),
                            total=g("total_time"), dwell=g("total_dwell_time"),
                            accel=g("total_accel_time"), coast=g("total_coast_time"),
                            decel=g("total_decel_time"), wait=g("total_wait_time"),
                            spawn=spawn))
    return out


def occupancy(rows, lo, hi):
    """Trains on the network over [lo, hi], by an exact sweep over the entry and
    exit instants. Every train is clipped to the window, including one that
    completes after it closes.

    Returns (time_average, peak, time_at_level), where peak is the largest number
    on the network at any instant and time_at_level[k] is the hours spent with
    exactly k on it. The sweep already visits every instant at which the count
    changes and the count is constant between them, so both come out of the same
    pass as the integral and cost nothing extra. The hourly profile cannot give
    either, because averaging within the hour smooths the instant away."""
    ev = []
    for r in rows:
        a, b = max(r["ready"], lo), min(r["done"], hi)
        if b > a:
            ev.append((a, 1))
            ev.append((b, -1))
    ev.sort()
    area = 0.0
    cur = 0
    peak = 0
    at = [0.0] * OCC_LEVELS
    prev = lo
    for t, d in ev:
        if t > prev:
            area += cur * (t - prev)
            at[min(cur, OCC_LEVELS - 1)] += t - prev
        prev = t
        cur += d
        if cur > peak:
            peak = cur
    if hi > prev:
        area += cur * (hi - prev)
        at[min(cur, OCC_LEVELS - 1)] += hi - prev
    return area / (hi - lo), peak, at


def profile(rows):
    """Trains on the network averaged within each hour, over the whole run."""
    out = [0.0] * PROFILE_HI
    for r in rows:
        a, b = r["ready"], min(r["done"], float(PROFILE_HI))
        if b <= a:
            continue
        i = int(a)
        while i < PROFILE_HI and i < b:
            lo_i, hi_i = max(a, float(i)), min(b, float(i + 1))
            if hi_i > lo_i:
                out[i] += hi_i - lo_i
            i += 1
    return out


def cell_dir(N, rate, mode):
    return os.path.join(RESULTS, mode, f"N{N:02d}", rate_label(rate))


def run_slice(N, rate, mode, sidx):
    wdir, frozen_name = setup(N, rate, mode, slice_idx=sidx)
    warm, pl = ("", 1) if mode == "engineered" else (frozen_name, 0)
    out_dir = cell_dir(N, rate, mode)
    os.makedirs(out_dir, exist_ok=True)
    part = os.path.join(out_dir, f"part_{sidx:02d}.json.gz")
    rng = random.Random(1000 + sidx)
    hist = {"wait": [0] * N_BINS, "spawn": [0] * N_BINS}
    below = {"wait": 0, "spawn": 0}
    tail, reservoir, per_seed = [], {"wait": [], "spawn": []}, []
    seen = {"wait": 0, "spawn": 0}
    excluded = 0
    # The hourly occupancy profile is accumulated rather than stored per
    # replication: a thousand profiles per slice is a lot of numbers to carry
    # for a mean and a band.
    prof_sum = [0.0] * PROFILE_HI
    prof_sq = [0.0] * PROFILE_HI
    prof_n = 0
    occ_time = [0.0] * OCC_LEVELS
    lo_w, hi_w = D._window(wdir)
    for i in range(SLICE):
        seed = H.SEED_START + sidx * SLICE + i
        res = D.run_one(wdir, seed, warm, pl)
        if res is None or res[0] != 0 or res[3] != 1:
            excluded += 1
            continue
        log = read_log(wdir)
        prof = profile(log)
        for b in range(PROFILE_HI):
            prof_sum[b] += prof[b]
            prof_sq[b] += prof[b] * prof[b]
        prof_n += 1
        n_on_network, peak_on_network, at_level = occupancy(log, lo_w, hi_w)
        for k, v in enumerate(at_level):
            occ_time[k] += v
        done_in = [r for r in log if lo_w <= r["done"] <= (hi_w if hi_w > 0 else r["done"])]
        comp = {c: sum(r[c] for r in done_in) for c in COMPONENTS}
        time_on_network = sum(r["total"] for r in done_in) / len(done_in) if done_in else 0.0
        ws, ss = [], []
        for wv, sv in trains(wdir):
            for key, val in (("wait", wv * 60.0), ("spawn", None if sv is None else sv * 60.0)):
                if val is None:
                    continue
                (ws if key == "wait" else ss).append(val)
                if val < NEGLIGIBLE_MIN:
                    below[key] += 1
                else:
                    hist[key][_bin(val)] += 1
                seen[key] += 1
                r = reservoir[key]
                if len(r) < RESERVOIR:
                    r.append(val)
                else:
                    j = rng.randrange(seen[key])
                    if j < RESERVOIR:
                        r[j] = val
            if wv * 60.0 >= TAIL_MIN:
                tail.append([seed, round(wv * 60.0, 3)])
        ws.sort(); ss.sort()
        per_seed.append([seed, len(ws), sum(1 for x in ws if x < NEGLIGIBLE_MIN),
                         round(sum(ws) / len(ws), 5) if ws else 0.0]
                        + [round(_pct(ws, q), 4) for q in PCTS]
                        + [round(ws[-1], 4) if ws else 0.0]
                        + [round(sum(ss) / len(ss), 5) if ss else 0.0,
                           round(ss[-1], 4) if ss else 0.0]
                        + [round(n_on_network, 5), peak_on_network, len(done_in),
                           round(time_on_network, 5)]
                        + [round(comp[c], 5) for c in COMPONENTS])
        if (i + 1) % 250 == 0:
            print(f"[{mode} N{N:02d}/r{rate:.2f} s{sidx:02d}] {i + 1}/{SLICE}", flush=True)
    with gzip.open(part, "wt") as fh:
        json.dump({"cell": f"N{N:02d}/r{rate:.2f}", "mode": mode, "slice": sidx,
                   "excluded": excluded, "seen": seen, "below": below,
                   "hist": hist, "tail": tail, "reservoir": reservoir,
                   "per_seed": per_seed, "window": [lo_w, hi_w],
                   "profile_sum": prof_sum, "profile_sq": prof_sq, "profile_n": prof_n,
                   "occ_time": occ_time,
                   "per_seed_cols": PER_SEED_COLS,
                   "bins": {"lo": BIN_LO, "hi": BIN_HI, "w": BIN_W},
                   "negligible_min": NEGLIGIBLE_MIN, "tail_min": TAIL_MIN}, fh)
    print(f"[{mode} N{N:02d}/r{rate:.2f} s{sidx:02d}] done, {excluded} excluded -> {part}",
          flush=True)


def merge(N, rate, mode):
    out_dir = cell_dir(N, rate, mode)
    agg = None
    for sidx in range(N_SLICES):
        p = os.path.join(out_dir, f"part_{sidx:02d}.json.gz")
        if not os.path.exists(p):
            raise FileNotFoundError(f"missing {p}; run every slice before merging")
        with gzip.open(p, "rt") as fh:
            d = json.load(fh)
        if agg is None:
            agg = d
            agg.pop("slice", None)
            continue
        agg["excluded"] += d["excluded"]
        for k in ("wait", "spawn"):
            agg["seen"][k] += d["seen"][k]
            agg["below"][k] += d["below"][k]
            agg["hist"][k] = [a + b for a, b in zip(agg["hist"][k], d["hist"][k])]
            agg["reservoir"][k] = agg["reservoir"][k] + d["reservoir"][k]
        agg["tail"] += d["tail"]
        agg["per_seed"] += d["per_seed"]
        agg["profile_n"] += d["profile_n"]
        agg["profile_sum"] = [a + b for a, b in zip(agg["profile_sum"], d["profile_sum"])]
        agg["profile_sq"] = [a + b for a, b in zip(agg["profile_sq"], d["profile_sq"])]
        # Parts written before occ_time existed merge without it rather than
        # failing, so a cell not yet re-run can still be re-merged.
        if "occ_time" in agg and "occ_time" in d:
            agg["occ_time"] = [a + b for a, b in zip(agg["occ_time"], d["occ_time"])]
        else:
            agg.pop("occ_time", None)
    # Relabel from PER_SEED_COLS rather than trust the copy part_00 stored, so
    # a rename reaches the merged cells without re-running the slices. The row
    # layout itself is unchanged, which the width check asserts.
    assert len(agg["per_seed"][0]) == len(PER_SEED_COLS), (
        f"row width {len(agg['per_seed'][0])} does not match "
        f"{len(PER_SEED_COLS)} column names")
    agg["per_seed_cols"] = PER_SEED_COLS
    out = os.path.join(out_dir, "cell.json.gz")
    with gzip.open(out, "wt") as fh:
        json.dump(agg, fh)
    print(f"[{mode} N{N:02d}/r{rate:.2f}] merged {len(agg['per_seed'])} replications, "
          f"{agg['seen']['wait']:,} trains, {len(agg['tail']):,} above "
          f"{TAIL_MIN:.0f} min, {agg['excluded']} excluded -> {out}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("slice", "merge"):
        q = sub.add_parser(name)
        q.add_argument("--N", type=int, required=True)
        q.add_argument("--rate", type=float, required=True)
        q.add_argument("--mode", required=True,
                       choices=["native", "union", "fullunion", "engineered"])
        if name == "slice":
            q.add_argument("--slice", type=int, required=True)
    a = ap.parse_args()
    if a.cmd == "slice":
        run_slice(a.N, a.rate, a.mode, a.slice)
    else:
        merge(a.N, a.rate, a.mode)


if __name__ == "__main__":
    main()
