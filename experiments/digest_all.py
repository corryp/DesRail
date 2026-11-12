#!/usr/bin/env python3
"""Compact digest of all drained-rerun table numbers (run on HPC where the trees
are clean).

Two path facts this predated, both of which made it print empty tables rather
than fail: the 2026-10-22 campaign splits every stage by stall policy, and the
corridor and overlength cells live under work/, not results/ (results/ holds the
stages that merge per-seed parts, which those two do not). The old single-policy
tree is parked at results/_pre_twoarm_20261023 and is not read here.

    work/corridor/<policy>/N<nn>/r<rate>/benchmark.csv
    work/overlength/<policy>/N<nn>/f<frac>/{pl,hybrid,engineered}_benchmark.csv
    results/toowoomba/<policy>/<mode>/b<b>_c0.50/training_log.csv
    results/toowoomba_hybrid_benchmark/<policy>/summary.csv
"""
import os, csv, statistics, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.join(HERE, "results")
W = os.path.join(HERE, "work")
POLICIES = ["discard", "bank"]


def col(rows, k):
    return [float(r[k]) for r in rows if r.get(k) not in (None, "")]


def corridor(pol):
    print("=" * 90)
    print(f"CORRIDOR BENCHMARK, stall policy = {pol} "
          "(learned vs PL, per-cell 100 seeds): dl counts + means")
    print(f"{'cell':<10} {'n':>4} {'dl_L':>5} {'dl_PL':>6} {'wait_L':>8} {'wait_PL':>8} "
          f"{'spwn_L':>8} {'thr_L':>7} {'thr_PL':>7}")
    for N in [2, 5, 10, 15, 20]:
        for r in [1.00, 1.25, 1.50, 1.75, 2.00, 2.25, 2.50, 2.75, 3.00]:
            p = f"{W}/corridor/{pol}/N{N:02d}/r{r:.2f}/benchmark.csv"
            if not os.path.exists(p):
                continue
            rows = list(csv.DictReader(open(p)))
            n = len(rows)
            dlL = sum(int(x["learned_deadlocked"]) for x in rows)
            dlP = sum(int(x["pl_deadlocked"]) for x in rows)
            okL = [x for x in rows if x["learned_deadlocked"] == "0"]
            okP = [x for x in rows if x["pl_deadlocked"] == "0"]
            wL = statistics.mean(col(okL, "avg_wait_learned")) if okL else 0
            wP = statistics.mean(col(okP, "avg_wait_pl")) if okP else 0
            sL = statistics.mean(col(okL, "avg_spawn_learned")) if okL else 0
            tL = statistics.mean(col(okL, "throughput_learned")) if okL else 0
            tP = statistics.mean(col(okP, "throughput_pl")) if okP else 0
            print(f"N{N:02d}/r{r:.2f} {n:>4} {dlL:>5} {dlP:>6} {wL:>8.4f} {wP:>8.4f} "
                  f"{sL:>8.4f} {tL:>7.3f} {tP:>7.3f}")


def overlength(pol):
    print("\n" + "=" * 90)
    print(f"OVERLENGTH, stall policy = {pol} -- per N/fraction: dl counts "
          "(PL / hybrid / engineered) + learned constraints + thr")
    print(f"{'cell':<11} {'seeds':>5} {'con':>5} {'dl_PL':>6} {'dl_hyb':>7} {'dl_eng':>7} "
          f"{'thr_hyb':>8} {'thr_eng':>8}")
    for N in [2, 5, 10, 15, 20]:
        for f in [0.10, 0.25, 0.50]:
            base = f"{W}/overlength/{pol}/N{N:02d}/f{f:.2f}"
            if not os.path.isdir(base):
                continue

            def dlc(name):
                p = f"{base}/{name}.csv"
                if not os.path.exists(p):
                    return ("-", 0, 0)
                rr = list(csv.DictReader(open(p)))
                dl = sum(int(x["deadlocked"]) for x in rr)
                ok = [x for x in rr if x["deadlocked"] == "0"]
                thr = statistics.mean(col(ok, "throughput")) if ok else 0
                return (len(rr), dl, thr)

            nP, dlP, _ = dlc("pl_benchmark")
            nH, dlH, thH = dlc("hybrid_benchmark")
            nE, dlE, thE = dlc("engineered_benchmark")
            tl = f"{base}/training_log.csv"
            con = "-"; seeds = "-"
            if os.path.exists(tl):
                tr = list(csv.reader(open(tl)))
                if len(tr) > 1:
                    seeds = tr[-1][0]
            cc = f"{base}/constraints.csv"
            if os.path.exists(cc):
                ids = set(x.split(",")[0] for x in open(cc)
                          if x.startswith("dl_") or x.startswith("ol_"))
                con = len(ids)
            print(f"N{N:02d}/f{f:.2f} {seeds:>5} {str(con):>5} {dlP:>6} {dlH:>7} {dlE:>7} "
                  f"{thH:>8.3f} {thE:>8.3f}")


def toowoomba(pol):
    print("\n" + "=" * 90)
    print(f"TOOWOOMBA b-sweep, stall policy = {pol}: final constraints "
          "(pure vs hybrid) per b")
    for mode in ["pure", "hybrid"]:
        line = f"  {mode:<7}"
        for b in [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]:
            tl = f"{R}/toowoomba/{pol}/{mode}/b{b:.2f}_c0.50/training_log.csv"
            c = "-"
            if os.path.exists(tl):
                tr = list(csv.reader(open(tl)))
                if len(tr) > 1:
                    try:
                        c = max(int(row[3]) for row in tr[1:]
                                if len(row) > 3 and row[3].isdigit())
                    except ValueError:
                        c = "?"
            line += f"  b{b:.2f}={c}"
        print(line)
    print("\n" + "=" * 90)
    print(f"TOOWOOMBA hybrid benchmark, stall policy = {pol} "
          "(lambda_B, lambda_C, thr, deadlocks):")
    p = f"{R}/toowoomba_hybrid_benchmark/{pol}/summary.csv"
    if os.path.exists(p):
        for r in csv.DictReader(open(p)):
            print(f"  lB={r['lambda_B']} lC={r['lambda_C']} "
                  f"thr={r['mean_throughput']} dl={r['deadlocks']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", nargs="+", default=POLICIES, choices=POLICIES,
                    help="stall policy arms to digest (default: both, discard first)")
    a = ap.parse_args()
    for pol in a.policy:
        corridor(pol)
        overlength(pol)
        toowoomba(pol)
        print()


if __name__ == "__main__":
    main()
