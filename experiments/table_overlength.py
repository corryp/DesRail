#!/usr/bin/env python3
"""Overlength experiment outcomes, per cell and per corridor size.

Table 4 of the paper. One row per corridor size, with the training columns and
the deadlock counts of the three benchmarked supervisors:

    work/overlength/<policy>/N<nn>/f<frac>/training_log.csv
    work/overlength/<policy>/N<nn>/f<frac>/pl_benchmark.csv
    work/overlength/<policy>/N<nn>/f<frac>/hybrid_benchmark.csv
    work/overlength/<policy>/N<nn>/f<frac>/engineered_benchmark.csv

The three benchmarks run the same 100 seeds, so their deadlock counts are
paired and a difference between them is a difference in the supervisor rather
than in the traffic. PL-only is the baseline the overlength geometry defeats;
hybrid is PL plus the learned set; engineered is PL plus the spawn-hold set.

Convergence follows the campaign's stopping rule, as in table_constraints.py: a
cell converged if its last 100 training seeds found no deadlock. The seed count
is reported because it is the quantity that moved between the stall policy arms,
in the opposite direction to the constraint count.

Means are over the deadlock-free seeds only. A replication that deadlocks stops
at detection, so its throughput covers just the trains that finished first, and
averaging that in with complete runs understates nothing and overstates the
rest.

    python3 table_overlength.py                      # print both arms
    python3 table_overlength.py --out overlength.csv # and write the long form
"""
import os, csv, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.join(HERE, "work")
POLICIES = ["discard", "bank"]
GRID_N = [2, 5, 10, 15, 20]
OVL_F = [0.10, 0.25, 0.50]
ARMS = ["pl", "hybrid", "engineered"]
CLEAN_WINDOW = 100
CAP = 20000


def cell_dir(pol, N, frac):
    return os.path.join(W, "overlength", pol, f"N{N:02d}", f"f{frac:.2f}")


def training(path):
    """seeds run, final constraint count, trailing clean seeds, status."""
    try:
        rows = list(csv.DictReader(open(path, newline="")))
    except OSError:
        return None
    if not rows:
        return None
    clean = 0
    for r in reversed(rows):
        if int(r["deadlock_found"]) == 0:
            clean += 1
        else:
            break
    total = int(rows[-1]["total_constraints"])
    status = ("converged" if clean >= CLEAN_WINDOW else
              "capped" if total > CAP else "not_converged")
    return dict(seeds=len(rows), total=total, clean=clean, status=status)


def benchmark(path):
    """seeds, deadlocks, and the means over the deadlock-free seeds."""
    try:
        rows = list(csv.DictReader(open(path, newline="")))
    except OSError:
        return None
    if not rows:
        return None
    dl = sum(int(r["deadlocked"]) for r in rows)
    clean = [r for r in rows if not int(r["deadlocked"])]
    def mean(k):
        v = [float(r[k]) for r in clean]
        return sum(v) / len(v) if v else float("nan")
    return dict(seeds=len(rows), dl=dl, n_clean=len(clean),
                thr=mean("throughput"), wait=mean("avg_wait_time"),
                cpu=mean("cpu_time"))


def collect(pol):
    out = {}
    for N in GRID_N:
        for frac in OVL_F:
            d = cell_dir(pol, N, frac)
            row = {"train": training(os.path.join(d, "training_log.csv"))}
            for arm in ARMS:
                row[arm] = benchmark(os.path.join(d, f"{arm}_benchmark.csv"))
            if row["train"] or any(row[a] for a in ARMS):
                out[(N, frac)] = row
    return out


def rng(vals):
    """A range, or a single value where the ends coincide."""
    lo, hi = min(vals), max(vals)
    return f"{lo:,}" if lo == hi else f"{lo:,}--{hi:,}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", nargs="*", default=POLICIES)
    ap.add_argument("--out")
    a = ap.parse_args()

    long_rows = []
    for pol in a.policy:
        got = collect(pol)
        if not got:
            print(f"no overlength cells under {os.path.join(W, 'overlength', pol)}")
            continue
        print("=" * 100)
        print(f"OVERLENGTH, stall policy = {pol}")
        print(f"\n  {'N':>2} {'phi':>5} {'cstr':>7} {'seeds':>6} {'status':>13}"
              f" {'PL DL':>6} {'Hyb DL':>7} {'Eng DL':>7}"
              f" {'PL thr':>7} {'Hyb thr':>8} {'Eng thr':>8}")
        for N in GRID_N:
            for frac in OVL_F:
                r = got.get((N, frac))
                if not r:
                    continue
                t = r["train"] or {}
                line = (f"  {N:>2} {frac:>5.2f} {t.get('total', 0):>7,}"
                        f" {t.get('seeds', 0):>6,} {t.get('status', '-'):>13}")
                for arm, w in zip(ARMS, (6, 7, 7)):
                    b = r[arm]
                    line += f" {(str(b['dl']) if b else '-'):>{w}}"
                for arm, w in zip(ARMS, (7, 8, 8)):
                    b = r[arm]
                    line += f" {(format(b['thr'], '.2f') if b else '-'):>{w}}"
                print(line)
                long_rows.append([pol, f"N{N:02d}", f"{frac:.2f}",
                                  t.get("total", ""), t.get("seeds", ""),
                                  t.get("clean", ""), t.get("status", "")]
                                 + [v for arm in ARMS for v in (
                                     (r[arm]["seeds"], r[arm]["dl"],
                                      f"{r[arm]['thr']:.4f}", f"{r[arm]['wait']:.6f}",
                                      f"{r[arm]['cpu']:.4f}")
                                     if r[arm] else ("", "", "", "", ""))])

        # The paper's table is one row per N, aggregated over the fractions, so
        # print the aggregation here rather than leaving it to be done by eye.
        print(f"\n  per-N, aggregated over phi in {OVL_F}")
        print(f"  {'N':>2} {'constraints':>17} {'seeds':>11} {'status':>13}"
              f" {'PL DL':>8} {'Hyb DL':>8} {'Eng DL':>8} {'seeds/row':>10}")
        for N in GRID_N:
            cells = [got[(N, f)] for f in OVL_F if (N, f) in got]
            if not cells:
                continue
            ts = [c["train"] for c in cells if c["train"]]
            st = sorted({t["status"] for t in ts}) or ["-"]
            line = (f"  {N:>2} {rng([t['total'] for t in ts]):>17}"
                    f" {rng([t['seeds'] for t in ts]):>11} {'/'.join(st):>13}")
            tot = 0
            for arm, w in zip(ARMS, (8, 8, 8)):
                bs = [c[arm] for c in cells if c[arm]]
                line += f" {(sum(b['dl'] for b in bs) if bs else '-'):>{w}}"
                tot = max(tot, sum(b["seeds"] for b in bs) if bs else 0)
            print(line + f" {tot:>10,}")
        print()

    if a.out and long_rows:
        with open(a.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["policy", "N", "phi", "constraints", "train_seeds",
                        "trailing_clean", "status"]
                       + [f"{arm}_{k}" for arm in ARMS
                          for k in ("seeds", "dl", "mean_throughput",
                                    "mean_wait", "mean_cpu")])
            w.writerows(long_rows)
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
