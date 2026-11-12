#!/usr/bin/env python3
"""Comment 1.m2: mean and 95th-percentile waiting time by direction and origin-destination pair, for the replication Figure 4 shows.

    fig4_delay_table.py --run DIR [--out DIR]        DIR holds before/ and after/, as
                                                     find_figure4_replication.py leaves them

WHAT THE COMMENT ASKS. "Quantify the directional imbalance illustrated in Figure 4 using mean and
95th-percentile waiting times by direction and origin-destination pair." Figure 4 is the Toowoomba
before-and-after train chart, so the statistics are Toowoomba per-route ones, and direction needs no
column of its own: with three terminals and six routes a pair names it, which is how
`entry_from_Brisbane>Gowrie_exit` reads as inbound. The mapping from pair to chainage direction is
derived from the chart the figure is drawn from rather than asserted, since that is what ties a row of
this table to a line on the panel.

WHICH PANEL. The left panel cannot be quantified and the response says so. `train_metric_log.csv`
records a train when it completes, and in a gridlocked run almost nothing does: on the published
replication 17 trains against 455. Waiting time for a permanently stalled train is not a long wait, it
is undefined, so the table is taken from the right-hand panel, which is also what the paragraph after
the figure is about.

WHICH WINDOW. Two are reported, because they answer different questions and reporting one as the other
would be the window-length confound that spoiled the spawn-delay comparison for comment 1.m1.

  [warmup, start_warmdown] is the run's own stats window, and the headline. In-network waiting is
  stationary over it: on the published replication the mean settles by t = 24 h and then holds to within
  a few thousandths for every subsequent 12 h block, so this is a steady-state figure and not an
  artefact of where the horizon was put.
  [0, PLOT_H] is the window the figure actually plots, reported for completeness and not for inference.
  It is the network filling and the deadlock forming, it holds a few dozen trips, and a 95th percentile
  over three or four observations per pair means nothing. The response gives the first and notes that
  the figure shows the first few hours of it.

ONE REPLICATION DOES NOT SETTLE THE DIRECTION, which is why --run is repeatable. The reviewer asks
about the imbalance Figure 4 illustrates, so the figure's own replication is reported first. But the
mean ordering between the two Brisbane-line directions flips between seeds: on seed 1 trains toward
Brisbane wait longer in the mean, on seed 150 trains from Brisbane do. The 95th percentile and the
maximum put the from-Brisbane direction worse on both. A claim about the junction's geometry is a claim
about the regime and not about one seed, so pass every replication and let the pooled distribution carry
it. Pooling is over trips, not over per-seed percentiles, which is the convention `summarise_trains`
already follows for the corridor: a percentile of percentiles is not a percentile of anything.

WHAT IS NOT REPORTED. Spawn delay, which is to say queueing to enter the network. Under the deliberate
over-saturation the entry queue grows without bound: on the published replication 6.5 trains/hr are
offered and 5.0 complete, and mean spawn delay climbs about 5 h per 12 h block for the whole run. It has
no steady-state value, so any figure for it is a statement about the horizon. The script prints it as a
diagnostic, marked, so that the growth is on the record rather than rediscovered.

Conventions come from `run_heldout_drained.py`: the same `_pctl`, the same destination parse, and the
same window read out of the run's own runctrl, so these numbers sit beside the campaign's other delay
figures without a conversion.
"""
from __future__ import annotations

import argparse
import collections
import csv
import gzip
import os
import statistics
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, CAMPAIGN_DIR)
os.environ.setdefault("STALL_POLICY", "bank")
from run_heldout_drained import _pctl, _dest, _window  # noqa: E402

PLOT_H = 10.0
BLOCK_H = 12.0          # block width for the stationarity check


def pair_directions(wdir):
    """(origin, destination) -> {corridor_id: 'increasing'|'decreasing'} from the chart.

    Per corridor, not per train: a junction-crossing train appears on both chainage
    frames, and taking its first and last sample across the two gives a direction that
    belongs to neither.
    """
    chart = os.path.join(wdir, "output", "train_chart.csv")
    ends = {}
    with open(chart, newline="") as f:
        for r in csv.DictReader(f):
            k = (r["train"], r["corridor_id"])
            c = float(r["chainage"])
            if k not in ends:
                ends[k] = [c, c]
            ends[k][1] = c
    od = {}
    with open(os.path.join(wdir, "output", "train_metric_log.csv"), newline="") as f:
        for r in csv.DictReader(f):
            od[r["id"]] = (r["start_loc"], _dest(r["job_list"]))
    out = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    for (t, corr), (first, last) in ends.items():
        if t in od:
            out[od[t]][corr]["increasing" if last > first else "decreasing"] += 1
    return {p: {c: cnt.most_common(1)[0][0] for c, cnt in per.items()}
            for p, per in out.items()}


def trips(wdir):
    """Per-trip rows from the run's own log, which may be compressed.

    An absent log used to return an empty list, so a pooled run whose logs had been
    archived would have reported fewer trips with nothing saying why. It raises instead:
    a sample silently missing a tenth of itself is worse than a stopped script.
    """
    rows = []
    plain = os.path.join(wdir, "output", "train_metric_log.csv")
    path = plain if os.path.exists(plain) else plain + ".gz"
    if not os.path.exists(path):
        raise FileNotFoundError(f"no train_metric_log.csv or .csv.gz under {wdir}/output")
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", newline="") as f:
        for r in csv.DictReader(f):
            raw = (r.get("spawn_delay") or "").strip()
            rows.append(dict(pair=(r["start_loc"], _dest(r["job_list"])),
                             done=float(r.get("completion_time", 0) or 0),
                             wait=float(r.get("total_wait_time", 0) or 0),
                             total=float(r.get("total_time", 0) or 0),
                             spawn=float(raw) if raw else None))
    return rows


def stats(vals):
    v = sorted(vals)
    return len(v), statistics.fmean(v), _pctl(v, 0.95), v[-1]


def table(rows, lo, hi, dirs, label, writer=None):
    # `label` lands in a CSV column, so it carries no comma: correct CSV quoting still
    # defeats a reader reaching for cut or awk, which is how a wrong-column comparison
    # passed silently in the archive manifest's binary check.
    sel = [r for r in rows if r["done"] >= lo and (hi <= 0 or r["done"] <= hi)]
    win = f"[{lo:g}, {hi:g}]" if hi > 0 else f"[{lo:g}, end]"
    print(f"\n=== {label}: window {win} h, {len(sel)} trips of {len(rows)} ===")
    if not sel:
        print("  nothing completed in the window")
        return
    by = collections.defaultdict(list)
    for r in sel:
        by[r["pair"]].append(r["wait"])
    print(f"  {'origin':>20} {'destination':>20} {'Brisbane-line dir':>18} "
          f"{'n':>5} {'mean':>7} {'p95':>7} {'max':>7}")
    for p in sorted(by):
        n, mean, p95, mx = stats(by[p])
        # corridor 0 is the Brisbane line: it carries only the Brisbane routes.
        d = dirs.get(p, {}).get("0", "n/a")
        print(f"  {p[0]:>20} {p[1]:>20} {d:>18} {n:>5} {mean:>7.3f} {p95:>7.3f} {mx:>7.3f}")
        if writer:
            writer.writerow([label, win, p[0], p[1], d, n, f"{mean:.4f}",
                             f"{p95:.4f}", f"{mx:.4f}"])
    n, mean, p95, mx = stats([r["wait"] for r in sel])
    print(f"  {'all':>20} {'':>20} {'':>18} {n:>5} {mean:>7.3f} {p95:>7.3f} {mx:>7.3f}")
    if writer:
        writer.writerow([label, win, "all", "", "", n, f"{mean:.4f}", f"{p95:.4f}", f"{mx:.4f}"])

    # Pooled the way the paper's sentence is stated: by direction on the Brisbane line.
    groups = {"toward Brisbane (increasing chainage)":
              [r["wait"] for r in sel if r["pair"][1] == "exit_to_Brisbane"],
              "from Brisbane (decreasing chainage)":
              [r["wait"] for r in sel if r["pair"][0] == "entry_from_Brisbane"],
              "Gowrie<->Wyreema (no Brisbane leg)":
              [r["wait"] for r in sel
               if r["pair"][0] != "entry_from_Brisbane" and r["pair"][1] != "exit_to_Brisbane"]}
    print("  pooled by direction on the Brisbane line:")
    for name, v in groups.items():
        if not v:
            continue
        n, mean, p95, mx = stats(v)
        print(f"    {name:<40} n={n:>4} mean {mean:>6.3f}  p95 {p95:>6.3f}  max {mx:>6.3f}")
        if writer:
            writer.writerow([label, win, name, "", "", n, f"{mean:.4f}",
                             f"{p95:.4f}", f"{mx:.4f}"])


def stationarity(rows, hi):
    """Mean wait and mean spawn delay per block, so the reader can see which is stationary."""
    print("\n--- per-block means: in-network wait against entry queueing ---")
    end = hi if hi > 0 else max((r["done"] for r in rows), default=0.0)
    lo = 0.0
    while lo < end:
        b = [r for r in rows if lo <= r["done"] < lo + BLOCK_H]
        if b:
            sp = [r["spawn"] for r in b if r["spawn"] is not None]
            print(f"  t in [{lo:5.0f},{lo + BLOCK_H:5.0f})  n={len(b):4d}  "
                  f"mean wait {statistics.fmean(r['wait'] for r in b):6.3f}  "
                  f"mean spawn delay {statistics.fmean(sp) if sp else 0:7.3f}  "
                  f"(spawn delay is a diagnostic, not a reported statistic)")
        lo += BLOCK_H


def direction_of(pair):
    if pair[1] == "exit_to_Brisbane":
        return "toward Brisbane (increasing chainage)"
    if pair[0] == "entry_from_Brisbane":
        return "from Brisbane (decreasing chainage)"
    return "Gowrie<->Wyreema (no Brisbane leg)"


def per_seed_spread(runs, writer=None):
    """Mean and p95 by direction, one line per replication, then pooled over trips.

    The point of the per-seed lines is to show whether the ordering the paper's sentence
    asserts is a property of the junction or of a seed. The pooled line is the answer to
    the comment; the lines above it are why one replication is not.
    """
    print("\n=== by direction, per replication and pooled over trips ===")
    groups = ["toward Brisbane (increasing chainage)",
              "from Brisbane (decreasing chainage)",
              "Gowrie<->Wyreema (no Brisbane leg)"]
    pooled = collections.defaultdict(list)
    print(f"  {'run':>26} " + " ".join(f"{g.split(' (')[0]:>30}" for g in groups))
    print(f"  {'':>26} " + " ".join(f"{'n   mean    p95':>30}" for _g in groups))
    for label, aft in runs:
        rows = trips(aft)
        lo, hi = _window(aft)
        sel = [r for r in rows if r["done"] >= lo and (hi <= 0 or r["done"] <= hi)]
        cells = []
        for g in groups:
            v = [r["wait"] for r in sel if direction_of(r["pair"]) == g]
            pooled[g] += v
            if v:
                n, mean, p95, _mx = stats(v)
                cells.append(f"{n:>4} {mean:>6.3f} {p95:>6.3f}".rjust(30))
            else:
                cells.append(f"{'':>30}")
        print(f"  {label:>26} " + " ".join(cells))
    print(f"  {'-' * 26} " + " ".join("-" * 30 for _g in groups))
    cells = []
    for g in groups:
        n, mean, p95, mx = stats(pooled[g])
        cells.append(f"{n:>4} {mean:>6.3f} {p95:>6.3f}".rjust(30))
        if writer:
            writer.writerow(["pooled-over-runs", "stats window", g, "", "", n,
                             f"{mean:.4f}", f"{p95:.4f}", f"{mx:.4f}"])
    print(f"  {'POOLED':>26} " + " ".join(cells))
    for g in groups:
        n, mean, p95, mx = stats(pooled[g])
        print(f"    {g:<40} n={n:>5} mean {mean:>6.3f}  p95 {p95:>6.3f}  max {mx:>6.3f}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True, action="append",
                   help="seed directory holding before/ and after/; repeatable. The first "
                        "is the figure's own replication and is reported in full.")
    p.add_argument("--out", default=None, help="where to write fig4_delay_table.csv")
    a = p.parse_args()

    for r in a.run:
        for d in (os.path.join(r, "after"), os.path.join(r, "before")):
            if not os.path.isdir(d):
                sys.exit(f"ERROR: {d} not found")

    primary = a.run[0]
    aft, bef = os.path.join(primary, "after"), os.path.join(primary, "before")
    lo, hi = _window(aft)
    dirs = pair_directions(aft)
    rows_a, rows_b = trips(aft), trips(bef)

    print(f"figure run     {primary}")
    if len(a.run) > 1:
        print(f"also pooled    {len(a.run) - 1} further replications")
    print(f"stats window   [{lo:g}, {hi:g}] h, from the run's own runctrl")
    print(f"left panel     {len(rows_b)} trips completed, against {len(rows_a)} on the right. "
          f"The left panel is not quantifiable: a stalled train never completes and so never "
          f"reaches the log.")
    print("\nchainage direction per route, read off the chart the figure is drawn from:")
    for pr in sorted(dirs):
        per = ", ".join(f"corridor {c}: {d}" for c, d in sorted(dirs[pr].items()))
        print(f"  {pr[0]:>20} -> {pr[1]:<20} {per}")

    out_dir = a.out or primary
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "fig4_delay_table.csv")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["panel_window", "window_h", "origin", "destination",
                    "brisbane_line_direction", "n", "mean_wait_h", "p95_wait_h", "max_wait_h"])
        table(rows_a, lo, hi, dirs, "after/stats-window", w)
        table(rows_a, 0.0, PLOT_H, dirs, "after/figure-window", w)
        table(rows_b, 0.0, 0.0, dirs, "before/degenerate", w)
        runs = [(os.path.basename(r.rstrip("/")), os.path.join(r, "after")) for r in a.run]
        per_seed_spread(runs, w)
    stationarity(rows_a, hi)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
