#!/usr/bin/env python3
"""Compact digest of all drained-rerun table numbers (run on HPC where results/ is clean)."""
import os, csv, glob, statistics
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def col(rows, k):
    return [float(r[k]) for r in rows if r.get(k) not in (None, "")]


# ---- Corridor benchmark (learned vs PL), all N x rate, 100 seeds ----
print("=" * 90)
print("CORRIDOR BENCHMARK (learned vs PL, per-cell 100 seeds): dl counts + means")
print(f"{'cell':<10} {'n':>4} {'dl_L':>5} {'dl_PL':>6} {'wait_L':>8} {'wait_PL':>8} {'spwn_L':>8} {'thr_L':>7} {'thr_PL':>7}")
for N in [2, 5, 10, 15, 20]:
    for r in [1.00, 1.25, 1.50, 1.75, 2.00, 2.25, 2.50, 2.75, 3.00]:
        p = f"{R}/corridor/N{N:02d}/r{r:.2f}/benchmark.csv"
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
        print(f"N{N:02d}/r{r:.2f} {n:>4} {dlL:>5} {dlP:>6} {wL:>8.4f} {wP:>8.4f} {sL:>8.4f} {tL:>7.3f} {tP:>7.3f}")

# ---- Overlength: per (N, fraction) 4 phases ----
print("\n" + "=" * 90)
print("OVERLENGTH per N/fraction: dl counts (PL / hybrid / engineered) + learned constraints + thr")
print(f"{'cell':<11} {'seeds':>5} {'con':>5} {'dl_PL':>6} {'dl_hyb':>7} {'dl_eng':>7} {'thr_hyb':>8} {'thr_eng':>8}")
for N in [2, 5, 10, 15, 20]:
    for f in [0.10, 0.25, 0.50]:
        base = f"{R}/overlength/N{N:02d}/f{f:.2f}"
        if not os.path.isdir(base):
            continue
        def dlc(name):
            p = f"{base}/{name}.csv"
            if not os.path.exists(p): return ("-", 0, 0)
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
                # last total_constraints col (index 3 in dlexp-style) -> use max dl id
        # constraints from constraints.csv if present
        cc = f"{base}/constraints.csv"
        if os.path.exists(cc):
            ids = set(x.split(",")[0] for x in open(cc) if x.startswith("dl_") or x.startswith("ol_"))
            con = len(ids)
        print(f"N{N:02d}/f{f:.2f} {seeds:>5} {str(con):>5} {dlP:>6} {dlH:>7} {dlE:>7} {thH:>8.3f} {thE:>8.3f}")

# ---- Toowoomba b-sweep convergence (pure vs hybrid) ----
print("\n" + "=" * 90)
print("TOOWOOMBA b-sweep: final constraints (pure vs hybrid) per b")
for mode in ["pure", "hybrid"]:
    line = f"  {mode:<7}"
    for b in [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]:
        tl = f"{R}/toowoomba_bsweep/{mode}/b{b:.2f}_c0.50/training_log.csv"
        c = "-"
        if os.path.exists(tl):
            tr = list(csv.reader(open(tl)))
            if len(tr) > 1:
                # total_constraints is last numeric; use max over rows of col index 3
                try:
                    c = max(int(row[3]) for row in tr[1:] if len(row) > 3 and row[3].isdigit())
                except ValueError:
                    c = "?"
        line += f"  b{b:.2f}={c}"
    print(line)

print("\n" + "=" * 90)
print("TOOWOOMBA hybrid benchmark summary (lambda_B, lambda_C, thr, deadlocks):")
p = f"{R}/toowoomba_hybrid_benchmark/summary.csv"
if os.path.exists(p):
    for r in csv.DictReader(open(p)):
        print(f"  lB={r['lambda_B']} lC={r['lambda_C']} thr={r['mean_throughput']} dl={r['deadlocks']}")
