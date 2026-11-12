#!/usr/bin/env python3
"""Paired delay comparison between the learned and engineered supervisors.

Table 4 of the paper. The published version reported each supervisor's mean
wait and spawn delay side by side over 100 benchmark seeds, which leaves the
reader to difference two numbers whose sampling error is unstated. This pairs
them: both supervisors run the same held-out replication, so the difference is
taken within a replication and the arrival realisation cancels.

A replication enters the comparison only if BOTH supervisors leave it
deadlock-free and it drains, since a deadlocked replication has no meaningful
steady-state delay. The engineered set deadlocks on none of them, so in
practice the learned arm sets the sample.

The interval is a 95% normal interval on the paired differences. With n above
nine thousand the t quantile is 1.9601 against the normal's 1.9600, so the
distinction does not reach the third decimal of anything reported here.

A caveat the window makes necessary: spawn delay accumulates while a train
waits off-network for its entry arc, so at a saturated cell its mean depends on
how long the measurement window is, in a way wait time does not. That is a
reason to report the paired difference, where the window is the same on both
sides, rather than the absolute figure alone.

    python3 table_delay.py --N 10
    python3 table_delay.py --N 10 --out delay.csv
"""
import os, sys, csv, math, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
GRID_R = [1.00, 1.25, 1.50, 1.75, 2.00, 2.25, 2.50, 2.75, 3.00]
Z95 = 1.959964


def cell_rows(pol, mode, N, rate):
    p = os.path.join(RES, "heldout_drained", pol, mode,
                     f"N{N:02d}", f"r{rate:.2f}", "heldout_raw.csv")
    if not os.path.exists(p):
        return None
    return {r["seed"]: r for r in csv.DictReader(open(p))}


def clean(r):
    return r.get("after_deadlock") == "0" and r.get("after_drained_clear") == "1"


def paired(pol, N, rate, learned="native"):
    """Per-seed (learned, engineered) pairs for the replications both clear."""
    L = cell_rows(pol, learned, N, rate)
    E = cell_rows(pol, "engineered", N, rate)
    if L is None or E is None:
        return None
    out = []
    for s, lr in L.items():
        er = E.get(s)
        if er is None or not clean(lr) or not clean(er):
            continue
        out.append((lr, er))
    return out


def stat(pairs, key):
    """mean under each supervisor, the paired difference, its 95% half-width,
    and the largest value either supervisor produced."""
    lv = [float(a[f"after_avg_{key}"]) for a, _ in pairs]
    ev = [float(b[f"after_avg_{key}"]) for _, b in pairs]
    d = [x - y for x, y in zip(lv, ev)]
    n = len(d)
    mean_d = sum(d) / n
    var = sum((x - mean_d) ** 2 for x in d) / (n - 1) if n > 1 else 0.0
    half = Z95 * math.sqrt(var / n) if n > 1 else 0.0
    mx_l = max(float(a[f"after_max_{key}"]) for a, _ in pairs)
    mx_e = max(float(b[f"after_max_{key}"]) for _, b in pairs)
    return dict(n=n, l=sum(lv) / n, e=sum(ev) / n, d=mean_d, half=half,
                max_l=mx_l, max_e=mx_e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--N", type=int, default=10)
    ap.add_argument("--policy", default="discard", choices=["discard", "bank"])
    ap.add_argument("--learned", default="native", choices=["native", "union", "fullunion"])
    ap.add_argument("--out")
    a = ap.parse_args()
    rows = []
    for metric, label in (("wait", "wait time"), ("spawn", "spawn delay")):
        print("=" * 104)
        print(f"{label.upper()}, N={a.N}, {a.learned} against engineered, "
              f"policy {a.policy}, hours")
        print(f"{'lambda':>7} {'n':>7} {'learned':>9} {'engineered':>11} "
              f"{'diff':>9} {'95% half':>9} {'max learned':>12} {'max eng':>9}")
        for rate in GRID_R:
            pairs = paired(a.policy, a.N, rate, a.learned)
            if not pairs:
                print(f"{rate:>7.2f} {'-':>7}")
                continue
            s = stat(pairs, metric)
            rows.append([metric, f"{rate:.2f}", s["n"], f"{s['l']:.4f}",
                         f"{s['e']:.4f}", f"{s['d']:.4f}", f"{s['half']:.4f}",
                         f"{s['max_l']:.3f}", f"{s['max_e']:.3f}"])
            print(f"{rate:>7.2f} {s['n']:>7,} {s['l']:>9.4f} {s['e']:>11.4f} "
                  f"{s['d']:>+9.4f} {s['half']:>9.4f} {s['max_l']:>12.3f} "
                  f"{s['max_e']:>9.3f}")
        print()
    if not rows:
        print(f"no paired cells under {os.path.join(RES, 'heldout_drained', a.policy)}")
        return 1
    if a.out:
        with open(a.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["metric", "rate", "n_paired", "mean_learned", "mean_engineered",
                        "mean_difference", "ci95_half_width", "max_learned", "max_engineered"])
            w.writerows(rows)
        print(f"wrote {len(rows)} rows to {a.out}")


if __name__ == "__main__":
    sys.exit(main() or 0)
