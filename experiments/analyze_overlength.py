#!/usr/bin/env python3
"""
Analyze overlength train experiment results.

Reads from results_overlength/ and produces summary tables showing
how PL constraint effectiveness degrades with overlength train fraction.

Usage:
    python analyze_overlength.py
    python analyze_overlength.py --results-dir path/to/results_overlength
"""

import argparse
import csv
import os
import sys


def load_csv(filepath):
    """Load a CSV file into a list of dicts."""
    if not os.path.exists(filepath):
        return []
    with open(filepath, newline="") as f:
        return list(csv.DictReader(f))


def summarize_benchmark(rows):
    """Compute summary stats from a benchmark CSV."""
    if not rows:
        return None
    n = len(rows)
    deadlocks = sum(1 for r in rows if int(r.get("deadlocked", 0)))
    throughputs = [float(r["throughput"]) for r in rows if float(r["throughput"]) > 0]
    wait_times = [float(r["avg_wait_time"]) for r in rows if float(r["throughput"]) > 0]
    cpu_times = [float(r["cpu_time"]) for r in rows]

    return {
        "seeds": n,
        "deadlocks": deadlocks,
        "deadlock_pct": 100.0 * deadlocks / n if n > 0 else 0,
        "mean_throughput": sum(throughputs) / len(throughputs) if throughputs else 0,
        "mean_wait": sum(wait_times) / len(wait_times) if wait_times else 0,
        "mean_cpu": sum(cpu_times) / len(cpu_times) if cpu_times else 0,
    }


def summarize_training(rows):
    """Compute summary stats from a training log CSV."""
    if not rows:
        return None
    total_seeds = len(rows)
    training_seeds = sum(1 for r in rows if int(r.get("new_constraints", 0)) > 0)
    total_constraints = int(rows[-1].get("total_constraints", 0))
    total_cpu = sum(float(r.get("cpu_time", 0)) for r in rows)

    # Check convergence
    consecutive_clean = 0
    for row in reversed(rows):
        if int(row.get("new_constraints", 0)) == 0:
            consecutive_clean += 1
        else:
            break
    converged = consecutive_clean >= 100

    return {
        "total_seeds": total_seeds,
        "training_seeds": training_seeds,
        "constraints": total_constraints,
        "cpu_time": total_cpu,
        "converged": converged,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Analyze overlength train experiment results")
    parser.add_argument("--results-dir", default=None,
                        help="Results directory (default: ./results_overlength)")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    results_dir = args.results_dir or os.path.join(script_dir, "results_overlength")

    if not os.path.exists(results_dir):
        print(f"Results directory not found: {results_dir}")
        sys.exit(1)

    # Discover scenarios
    scenarios = []
    for rate_dir in sorted(os.listdir(results_dir)):
        if not rate_dir.startswith("r"):
            continue
        rate_path = os.path.join(results_dir, rate_dir)
        if not os.path.isdir(rate_path):
            continue
        for frac_dir in sorted(os.listdir(rate_path)):
            if not frac_dir.startswith("f"):
                continue
            frac_path = os.path.join(rate_path, frac_dir)
            if not os.path.isdir(frac_path):
                continue
            rate = float(rate_dir[1:])
            frac = float(frac_dir[1:])
            scenarios.append((rate, frac, frac_path))

    if not scenarios:
        print("No scenarios found.")
        return

    # Group by rate
    rates = sorted(set(r for r, _, _ in scenarios))

    for rate in rates:
        print(f"\n{'='*80}")
        print(f"  Rate = {rate:.2f} trains/hr per direction")
        print(f"{'='*80}")

        # Header
        print(f"\n{'Frac':>6s}  {'PL DL':>6s}  {'PL DL%':>6s}  "
              f"{'PL thr':>7s}  {'PL wait':>8s}  "
              f"{'Cstrs':>6s}  {'Conv':>5s}  "
              f"{'Lrn DL':>6s}  {'Lrn thr':>7s}  {'Lrn wait':>8s}")
        print("-" * 80)

        for r, frac, path in scenarios:
            if r != rate:
                continue

            pl_rows = load_csv(os.path.join(path, "pl_benchmark.csv"))
            tr_rows = load_csv(os.path.join(path, "training_log.csv"))
            lr_rows = load_csv(os.path.join(path, "learned_benchmark.csv"))

            pl = summarize_benchmark(pl_rows)
            tr = summarize_training(tr_rows)
            lr = summarize_benchmark(lr_rows)

            # PL columns
            if pl:
                pl_dl = f"{pl['deadlocks']:>6d}"
                pl_pct = f"{pl['deadlock_pct']:>5.1f}%"
                pl_thr = f"{pl['mean_throughput']:>7.2f}"
                pl_wait = f"{pl['mean_wait']:>8.2f}"
            else:
                pl_dl = pl_pct = pl_thr = pl_wait = "   -  "

            # Training columns
            if tr:
                cstrs = f"{tr['constraints']:>6d}"
                conv = "  yes" if tr['converged'] else "   no"
            else:
                cstrs = "     -"
                conv = "    -"

            # Learned columns
            if lr:
                lr_dl = f"{lr['deadlocks']:>6d}"
                lr_thr = f"{lr['mean_throughput']:>7.2f}"
                lr_wait = f"{lr['mean_wait']:>8.2f}"
            else:
                lr_dl = lr_thr = lr_wait = "     -"

            print(f"{frac:>6.2f}  {pl_dl}  {pl_pct}  "
                  f"{pl_thr}  {pl_wait}  "
                  f"{cstrs}  {conv}  "
                  f"{lr_dl}  {lr_thr}  {lr_wait}")

    print()


if __name__ == "__main__":
    main()
