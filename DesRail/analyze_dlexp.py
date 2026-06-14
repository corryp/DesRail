"""
Analyze dlexp_log.csv from deadlock avoidance experiments.

Usage:
    python analyze_dlexp.py                          # default: ./output/dlexp_log.csv
    python analyze_dlexp.py path/to/dlexp_log.csv
"""

import csv
import sys
import re
from collections import defaultdict


def parse_constraint_details(details_str):
    """Parse 'dl_17(6);dl_18(3)' or legacy 'dl_17(3|6);dl_18(2|3)' into list of (id, segs)."""
    if not details_str:
        return []
    constraints = []
    for part in details_str.split(";"):
        m = re.match(r"(dl_\d+)\((\d+)\)", part.strip())
        if m:
            constraints.append((m.group(1), int(m.group(2))))
            continue
        m = re.match(r"(dl_\d+)\((\d+)\|(\d+)\)", part.strip())
        if m:
            constraints.append((m.group(1), int(m.group(3))))
    return constraints


def load_log(path):
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({
                "iteration": int(r["iteration"]),
                "deadlock_found": int(r["deadlock_found"]),
                "deadlock_time": float(r["deadlock_time"]),
                "new_constraints": int(r["new_constraints"]),
                "constraint_details": r["constraint_details"],
                "total_constraints": int(r["total_constraints"]),
                "trains_completed": int(r["trains_completed"]),
                "trains_per_hr": float(r["trains_per_hr"]),
                "sim_run_time": float(r["sim_run_time"]),
            })
    return rows


def find_plateaus(rows, min_duration=3):
    """Group consecutive iterations with same deadlock_time."""
    plateaus = []
    if not rows:
        return plateaus

    start = 0
    for i in range(1, len(rows)):
        if rows[i]["deadlock_time"] != rows[start]["deadlock_time"] or rows[i]["deadlock_found"] != rows[start]["deadlock_found"]:
            duration = i - start
            if duration >= min_duration:
                tph_values = [rows[j]["trains_per_hr"] for j in range(start, i)]
                plateaus.append({
                    "start_iter": rows[start]["iteration"],
                    "end_iter": rows[i - 1]["iteration"],
                    "duration": duration,
                    "deadlock_time": rows[start]["deadlock_time"],
                    "deadlock_found": rows[start]["deadlock_found"],
                    "avg_trains_hr": sum(tph_values) / len(tph_values),
                    "constraints_at_start": rows[start]["total_constraints"] - rows[start]["new_constraints"],
                    "constraints_at_end": rows[i - 1]["total_constraints"],
                })
            start = i

    # Final group
    duration = len(rows) - start
    if duration >= min_duration:
        tph_values = [rows[j]["trains_per_hr"] for j in range(start, len(rows))]
        plateaus.append({
            "start_iter": rows[start]["iteration"],
            "end_iter": rows[-1]["iteration"],
            "duration": duration,
            "deadlock_time": rows[start]["deadlock_time"],
            "deadlock_found": rows[start]["deadlock_found"],
            "avg_trains_hr": sum(tph_values) / len(tph_values),
            "constraints_at_start": rows[start]["total_constraints"] - rows[start]["new_constraints"],
            "constraints_at_end": rows[-1]["total_constraints"],
        })

    return plateaus


def find_regressions(rows, min_delta=2):
    """Find iterations where deadlock_time drops significantly."""
    regressions = []
    for i in range(1, len(rows)):
        if rows[i]["deadlock_found"] and rows[i - 1]["deadlock_found"]:
            delta = rows[i]["deadlock_time"] - rows[i - 1]["deadlock_time"]
            if delta <= -min_delta:
                # Find what constraint was added in the previous iteration
                prev_details = rows[i - 1]["constraint_details"]
                regressions.append({
                    "iteration": rows[i]["iteration"],
                    "from_time": rows[i - 1]["deadlock_time"],
                    "to_time": rows[i]["deadlock_time"],
                    "delta": delta,
                    "trigger_constraint": prev_details,
                    "total_constraints": rows[i]["total_constraints"],
                })
    return regressions


def constraint_size_stats(rows):
    """Analyze constraint limit and arc counts over time."""
    all_constraints = []
    for r in rows:
        for cid, segs in parse_constraint_details(r["constraint_details"]):
            all_constraints.append({
                "iteration": r["iteration"],
                "id": cid,
                "segs": segs,
                "deadlock_time": r["deadlock_time"],
            })
    return all_constraints


def print_summary(rows):
    print("=" * 70)
    print("DEADLOCK AVOIDANCE EXPERIMENT SUMMARY")
    print("=" * 70)

    deadlock_rows = [r for r in rows if r["deadlock_found"]]
    clean_rows = [r for r in rows if not r["deadlock_found"]]

    print(f"\nIterations:          {len(rows)}")
    print(f"  Deadlock:          {len(deadlock_rows)}")
    print(f"  Clean:             {len(clean_rows)}")
    print(f"Total constraints:   {rows[-1]['total_constraints']}")
    total_wall_time = sum(r["sim_run_time"] for r in rows)
    print(f"Total wall time:     {total_wall_time:.1f}s")

    if deadlock_rows:
        peak_time = max(r["deadlock_time"] for r in deadlock_rows)
        peak_iter = next(r["iteration"] for r in deadlock_rows if r["deadlock_time"] == peak_time)
        final_time = rows[-1]["deadlock_time"]
        print(f"\nPeak deadlock time:  t={peak_time:.0f} (iteration {peak_iter})")
        print(f"Final deadlock time: t={final_time:.0f} (iteration {rows[-1]['iteration']})")

        peak_trains = max(r["trains_completed"] for r in rows)
        peak_trains_iter = next(r["iteration"] for r in rows if r["trains_completed"] == peak_trains)
        print(f"Peak trains:         {peak_trains} (iteration {peak_trains_iter})")
        print(f"Final trains:        {rows[-1]['trains_completed']}")

    if clean_rows:
        r = clean_rows[-1]
        print(f"\nConverged at iteration {r['iteration']}")
        print(f"  Trains completed:  {r['trains_completed']}")
        print(f"  Trains/hr:         {r['trains_per_hr']:.2f}")
        print(f"  Constraints:       {r['total_constraints']}")


def print_plateaus(plateaus):
    print("\n" + "=" * 70)
    print("PLATEAUS (3+ consecutive iterations at same deadlock_time)")
    print("=" * 70)
    print(f"\n{'Iters':>12s}  {'Dur':>4s}  {'DL time':>8s}  {'Avg t/hr':>8s}  {'Constraints':>14s}")
    print("-" * 55)
    for p in plateaus:
        if not p["deadlock_found"]:
            continue
        print(f"{p['start_iter']:>5d}-{p['end_iter']:<5d}  {p['duration']:>4d}  {p['deadlock_time']:>8.0f}"
              f"  {p['avg_trains_hr']:>8.2f}  {p['constraints_at_start']:>5d} -> {p['constraints_at_end']:<5d}")


def print_regressions(regressions):
    print("\n" + "=" * 70)
    print("REGRESSIONS (deadlock_time drops by 2+)")
    print("=" * 70)
    if not regressions:
        print("\n  None found.")
        return
    print(f"\n{'Iter':>6s}  {'From':>8s}  {'To':>8s}  {'Delta':>8s}  {'Constraints':>6s}  Trigger")
    print("-" * 70)
    for reg in regressions:
        trigger = reg["trigger_constraint"]
        if len(trigger) > 30:
            trigger = trigger[:27] + "..."
        print(f"{reg['iteration']:>6d}  {reg['from_time']:>8.0f}  {reg['to_time']:>8.0f}"
              f"  {reg['delta']:>8.0f}  {reg['total_constraints']:>6d}  {trigger}")


def print_constraint_trends(all_constraints):
    print("\n" + "=" * 70)
    print("CONSTRAINT SIZE TRENDS")
    print("=" * 70)

    if not all_constraints:
        print("\n  No constraints generated.")
        return

    # Bucket by deadlock_time ranges
    buckets = defaultdict(list)
    for c in all_constraints:
        t = c["deadlock_time"]
        if t < 50:
            bucket = "t<50"
        elif t < 100:
            bucket = "t=50-99"
        elif t < 200:
            bucket = "t=100-199"
        elif t < 500:
            bucket = "t=200-499"
        elif t < 1000:
            bucket = "t=500-999"
        else:
            bucket = "t=1000+"
        buckets[bucket].append(c)

    bucket_order = ["t<50", "t=50-99", "t=100-199", "t=200-499", "t=500-999", "t=1000+"]
    print(f"\n{'DL time range':>14s}  {'Count':>6s}  {'Avg segs':>10s}  {'Max segs':>10s}")
    print("-" * 48)
    for bucket in bucket_order:
        if bucket not in buckets:
            continue
        constraints = buckets[bucket]
        avg_segs = sum(c["segs"] for c in constraints) / len(constraints)
        max_segs = max(c["segs"] for c in constraints)
        print(f"{bucket:>14s}  {len(constraints):>6d}  {avg_segs:>10.1f}  {max_segs:>10d}")

    # 2-seg constraints (mutex pairs — known regression triggers)
    small = [c for c in all_constraints if c["segs"] == 2]
    if small:
        print(f"\n2-seg constraints (mutex pairs): {len(small)}")
        for c in small:
            print(f"  {c['id']} at iteration {c['iteration']}, deadlock_time={c['deadlock_time']:.0f}")


def print_trains_per_hr_trend(rows):
    print("\n" + "=" * 70)
    print("TRAINS/HR BY CONSTRAINT COUNT")
    print("=" * 70)

    # Sample at regular constraint count intervals
    deadlock_rows = [r for r in rows if r["deadlock_found"]]
    if not deadlock_rows:
        print("\n  No deadlock iterations to analyze.")
        return

    max_constraints = deadlock_rows[-1]["total_constraints"]
    step = max(1, max_constraints // 15)

    print(f"\n{'Constraints':>12s}  {'Iter':>6s}  {'DL time':>8s}  {'Trains':>7s}  {'Trains/hr':>10s}")
    print("-" * 50)

    last_printed = -step
    for r in deadlock_rows:
        if r["total_constraints"] - last_printed >= step:
            print(f"{r['total_constraints']:>12d}  {r['iteration']:>6d}  {r['deadlock_time']:>8.0f}"
                  f"  {r['trains_completed']:>7d}  {r['trains_per_hr']:>10.2f}")
            last_printed = r["total_constraints"]

    # Always print last row
    r = deadlock_rows[-1]
    if r["total_constraints"] != last_printed:
        print(f"{r['total_constraints']:>12d}  {r['iteration']:>6d}  {r['deadlock_time']:>8.0f}"
              f"  {r['trains_completed']:>7d}  {r['trains_per_hr']:>10.2f}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "./output/dlexp_log.csv"

    try:
        rows = load_log(path)
    except FileNotFoundError:
        print(f"Error: {path} not found")
        sys.exit(1)

    if not rows:
        print("No data in log file.")
        sys.exit(1)

    print_summary(rows)

    plateaus = find_plateaus(rows)
    print_plateaus(plateaus)

    regressions = find_regressions(rows)
    print_regressions(regressions)

    all_constraints = constraint_size_stats(rows)
    print_constraint_trends(all_constraints)

    print_trains_per_hr_trend(rows)

    print()


if __name__ == "__main__":
    main()
