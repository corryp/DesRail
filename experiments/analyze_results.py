#!/usr/bin/env python3
"""
Aggregate and summarize results from the experiment campaign.

Reads training_log.csv and benchmark.csv from each scenario directory,
produces summary CSVs and prints formatted tables.

Usage:
    python analyze_results.py                  # Default: ./results/
    python analyze_results.py --results-dir path/to/results
"""

import argparse
import csv
import os
import re
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RESULTS = os.path.join(SCRIPT_DIR, "results")

SCENARIO_PATTERN = re.compile(r"N(\d+)")
RATE_PATTERN = re.compile(r"r(\d+\.\d+)")


def find_scenarios(results_dir):
    """Discover all (N, rate) scenarios under results_dir."""
    scenarios = []
    if not os.path.isdir(results_dir):
        return scenarios

    for n_dir in sorted(os.listdir(results_dir)):
        n_match = SCENARIO_PATTERN.match(n_dir)
        if not n_match:
            continue
        N = int(n_match.group(1))
        n_path = os.path.join(results_dir, n_dir)
        if not os.path.isdir(n_path):
            continue

        for r_dir in sorted(os.listdir(n_path)):
            r_match = RATE_PATTERN.match(r_dir)
            if not r_match:
                continue
            rate = float(r_match.group(1))
            r_path = os.path.join(n_path, r_dir)
            if os.path.isdir(r_path):
                scenarios.append((N, rate, r_path))

    return scenarios


def load_csv(filepath):
    """Load a CSV file into a list of dicts."""
    if not os.path.exists(filepath):
        return []
    with open(filepath, newline="") as f:
        return list(csv.DictReader(f))


def analyze_training(rows):
    """Compute training summary stats from training_log rows."""
    if not rows:
        return None

    total_seeds = len(rows)
    training_seeds = sum(1 for r in rows if int(r["new_constraints"]) > 0)
    final_constraints = int(rows[-1]["total_constraints"])
    total_cpu = sum(float(r["cpu_time"]) for r in rows)
    total_iterations = sum(int(r["iterations"]) for r in rows)

    # Throughput from the last clean seed (most representative)
    clean_rows = [r for r in rows if int(r["new_constraints"]) == 0]
    final_throughput = float(clean_rows[-1]["trains_per_hr"]) if clean_rows else 0.0

    # Consecutive clean seeds at end of training
    consecutive_clean = 0
    for r in reversed(rows):
        if int(r["new_constraints"]) == 0:
            consecutive_clean += 1
        else:
            break

    converged = consecutive_clean >= 100

    return {
        "total_seeds": total_seeds,
        "training_seeds": training_seeds,
        "final_constraints": final_constraints,
        "total_iterations": total_iterations,
        "total_cpu_time": total_cpu,
        "final_throughput": final_throughput,
        "consecutive_clean": consecutive_clean,
        "converged": converged,
    }


def analyze_benchmark(rows):
    """Compute benchmark summary stats from benchmark.csv rows."""
    if not rows:
        return None

    n = len(rows)
    thr_learned = [float(r["throughput_learned"]) for r in rows
                   if r["throughput_learned"]]
    thr_pl = [float(r["throughput_pl"]) for r in rows
              if r["throughput_pl"]]
    cpu_learned = [float(r["cpu_learned"]) for r in rows
                   if r["cpu_learned"]]
    cpu_pl = [float(r["cpu_pl"]) for r in rows
              if r["cpu_pl"]]
    wait_learned = [float(r["avg_wait_learned"]) for r in rows
                    if r.get("avg_wait_learned")]
    wait_pl = [float(r["avg_wait_pl"]) for r in rows
               if r.get("avg_wait_pl")]
    spawn_learned = [float(r["avg_spawn_learned"]) for r in rows
                     if r.get("avg_spawn_learned")]
    spawn_pl = [float(r["avg_spawn_pl"]) for r in rows
                if r.get("avg_spawn_pl")]
    pl_deadlocks = sum(1 for r in rows
                       if r.get("pl_deadlocked") == "1")
    learned_deadlocks = sum(1 for r in rows
                            if r.get("learned_deadlocked") == "1")

    def mean(lst):
        return sum(lst) / len(lst) if lst else 0.0

    def std(lst):
        if len(lst) < 2:
            return 0.0
        m = mean(lst)
        return (sum((x - m) ** 2 for x in lst) / (len(lst) - 1)) ** 0.5

    return {
        "n_seeds": n,
        "mean_throughput_learned": mean(thr_learned),
        "std_throughput_learned": std(thr_learned),
        "mean_throughput_pl": mean(thr_pl),
        "std_throughput_pl": std(thr_pl),
        "mean_wait_learned": mean(wait_learned),
        "std_wait_learned": std(wait_learned),
        "mean_wait_pl": mean(wait_pl),
        "std_wait_pl": std(wait_pl),
        "mean_cpu_learned": mean(cpu_learned),
        "mean_cpu_pl": mean(cpu_pl),
        "mean_spawn_learned": mean(spawn_learned),
        "std_spawn_learned": std(spawn_learned),
        "mean_spawn_pl": mean(spawn_pl),
        "std_spawn_pl": std(spawn_pl),
        "pl_deadlock_count": pl_deadlocks,
        "learned_deadlock_count": learned_deadlocks,
    }


def write_training_summary(scenarios_data, output_path):
    """Write training_summary.csv."""
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["N", "rate", "total_seeds", "training_seeds",
                     "final_constraints", "total_iterations",
                     "total_training_time", "final_throughput",
                     "consecutive_clean", "converged"])
        for N, rate, stats in scenarios_data:
            if stats is None:
                continue
            w.writerow([N, f"{rate:.2f}", stats["total_seeds"],
                        stats["training_seeds"], stats["final_constraints"],
                        stats["total_iterations"],
                        f"{stats['total_cpu_time']:.1f}",
                        f"{stats['final_throughput']:.4f}",
                        stats["consecutive_clean"],
                        1 if stats["converged"] else 0])


def write_benchmark_summary(scenarios_data, output_path):
    """Write benchmark_summary.csv."""
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["N", "rate", "n_seeds",
                     "mean_throughput_learned", "std_throughput_learned",
                     "mean_throughput_pl", "std_throughput_pl",
                     "mean_wait_learned", "std_wait_learned",
                     "mean_wait_pl", "std_wait_pl",
                     "mean_spawn_learned", "std_spawn_learned",
                     "mean_spawn_pl", "std_spawn_pl",
                     "mean_cpu_learned", "mean_cpu_pl",
                     "pl_deadlock_count", "learned_deadlock_count"])
        for N, rate, stats in scenarios_data:
            if stats is None:
                continue
            w.writerow([
                N, f"{rate:.2f}", stats["n_seeds"],
                f"{stats['mean_throughput_learned']:.4f}",
                f"{stats['std_throughput_learned']:.4f}",
                f"{stats['mean_throughput_pl']:.4f}",
                f"{stats['std_throughput_pl']:.4f}",
                f"{stats['mean_wait_learned']:.6f}",
                f"{stats['std_wait_learned']:.6f}",
                f"{stats['mean_wait_pl']:.6f}",
                f"{stats['std_wait_pl']:.6f}",
                f"{stats['mean_spawn_learned']:.6f}",
                f"{stats['std_spawn_learned']:.6f}",
                f"{stats['mean_spawn_pl']:.6f}",
                f"{stats['std_spawn_pl']:.6f}",
                f"{stats['mean_cpu_learned']:.3f}",
                f"{stats['mean_cpu_pl']:.3f}",
                stats["pl_deadlock_count"],
                stats["learned_deadlock_count"],
            ])


def print_training_table(training_data):
    """Print formatted training summary table."""
    print("\n" + "=" * 110)
    print("TRAINING SUMMARY")
    print("=" * 110)
    print(f"{'N':>4s}  {'Rate':>5s}  {'Seeds':>6s}  {'Train':>6s}  "
          f"{'Constr':>7s}  {'Iters':>7s}  {'CPU(s)':>9s}  {'Thr(t/h)':>8s}  "
          f"{'Clean':>6s}  {'Status':>8s}")
    print("-" * 110)

    for N, rate, stats in training_data:
        if stats is None:
            print(f"{N:>4d}  {rate:>5.2f}  {'(no data)':>6s}")
            continue
        status = "OK" if stats["converged"] else "TIMEOUT"
        print(f"{N:>4d}  {rate:>5.2f}  {stats['total_seeds']:>6d}  "
              f"{stats['training_seeds']:>6d}  "
              f"{stats['final_constraints']:>7d}  "
              f"{stats['total_iterations']:>7d}  "
              f"{stats['total_cpu_time']:>9.1f}  "
              f"{stats['final_throughput']:>8.4f}  "
              f"{stats['consecutive_clean']:>6d}  "
              f"{status:>8s}")


def print_benchmark_table(benchmark_data):
    """Print formatted benchmark summary table."""
    print("\n" + "=" * 170)
    print("BENCHMARK SUMMARY (Learned vs PL Constraints)")
    print("=" * 170)
    print(f"{'N':>4s}  {'Rate':>5s}  {'Seeds':>5s}  "
          f"{'Learned t/h':>11s}  {'PL t/h':>11s}  "
          f"{'Wait learn':>12s}  {'Wait PL':>12s}  "
          f"{'Spawn learn':>12s}  {'Spawn PL':>12s}  "
          f"{'CPU learn':>10s}  {'CPU PL':>10s}  "
          f"{'PL DL':>5s}  {'Learn DL':>8s}")
    print("-" * 170)

    for N, rate, stats in benchmark_data:
        if stats is None:
            print(f"{N:>4d}  {rate:>5.2f}  {'(no data)':>5s}")
            continue
        learned_str = (f"{stats['mean_throughput_learned']:.2f}"
                       f"+/-{stats['std_throughput_learned']:.2f}")
        pl_str = (f"{stats['mean_throughput_pl']:.2f}"
                  f"+/-{stats['std_throughput_pl']:.2f}")
        wait_l_str = (f"{stats['mean_wait_learned']:.3f}"
                      f"+/-{stats['std_wait_learned']:.3f}")
        wait_p_str = (f"{stats['mean_wait_pl']:.3f}"
                      f"+/-{stats['std_wait_pl']:.3f}")
        spawn_l_str = (f"{stats['mean_spawn_learned']:.3f}"
                       f"+/-{stats['std_spawn_learned']:.3f}")
        spawn_p_str = (f"{stats['mean_spawn_pl']:.3f}"
                       f"+/-{stats['std_spawn_pl']:.3f}")
        print(f"{N:>4d}  {rate:>5.2f}  {stats['n_seeds']:>5d}  "
              f"{learned_str:>11s}  {pl_str:>11s}  "
              f"{wait_l_str:>12s}  {wait_p_str:>12s}  "
              f"{spawn_l_str:>12s}  {spawn_p_str:>12s}  "
              f"{stats['mean_cpu_learned']:>10.2f}  "
              f"{stats['mean_cpu_pl']:>10.2f}  "
              f"{stats['pl_deadlock_count']:>5d}  "
              f"{stats['learned_deadlock_count']:>8d}")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze experiment campaign results")
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS,
                        help="Path to results directory")
    args = parser.parse_args()

    scenarios = find_scenarios(args.results_dir)
    if not scenarios:
        print(f"No scenarios found in {args.results_dir}")
        return

    print(f"Found {len(scenarios)} scenarios in {args.results_dir}")

    # Analyze training
    training_data = []
    for N, rate, path in scenarios:
        rows = load_csv(os.path.join(path, "training_log.csv"))
        stats = analyze_training(rows)
        training_data.append((N, rate, stats))

    # Analyze benchmark
    benchmark_data = []
    for N, rate, path in scenarios:
        rows = load_csv(os.path.join(path, "benchmark.csv"))
        stats = analyze_benchmark(rows)
        benchmark_data.append((N, rate, stats))

    # Print tables
    print_training_table(training_data)
    print_benchmark_table(benchmark_data)

    # Write summary CSVs
    summary_dir = os.path.join(args.results_dir, "summary")
    os.makedirs(summary_dir, exist_ok=True)

    training_csv = os.path.join(summary_dir, "training_summary.csv")
    write_training_summary(training_data, training_csv)
    print(f"\nWrote {training_csv}")

    benchmark_csv = os.path.join(summary_dir, "benchmark_summary.csv")
    write_benchmark_summary(benchmark_data, benchmark_csv)
    print(f"Wrote {benchmark_csv}")


if __name__ == "__main__":
    main()
