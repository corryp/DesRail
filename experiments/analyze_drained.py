#!/usr/bin/env python3
"""Full results check for the drained rerun. Reports per-(N,rate) for each arm:
residual-risk (after_deadlock rate), late(>190h) vs mid split of protected
residuals, throughput over deadlock-free seeds only, safe-seed counts, anomalies;
plus coreach no-op fractions. Emits FLAGS for anything that should halt the
paper update (coreach divergence, late blind-spot residual, anomalies, missing
cells)."""
import os, csv, glob, statistics
RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
HD = os.path.join(RES, "heldout_drained")
CO = os.path.join(RES, "coreach_drained")
GRID_N = [2, 5, 10, 15, 20]
GRID_R = [1.00, 2.00, 2.50, 2.75, 3.00]
MODES = ["native", "union", "engineered"]
flags = []


def load(mode, N, rate):
    p = os.path.join(HD, mode, f"N{N:02d}", f"r{rate:.2f}", "heldout_raw.csv")
    if not os.path.exists(p):
        return None
    return list(csv.DictReader(open(p)))


def arm_stats(rows, pre):
    """pre = 'after_' or 'before_'. Returns dict or None if column empty."""
    vals = [r for r in rows if r.get(pre + "deadlock", "") != ""]
    if not vals:
        return None
    n = len(vals)
    dl = [r for r in vals if r[pre + "deadlock"] == "1"]
    clear = [r for r in vals if r[pre + "deadlock"] == "0" and r.get(pre + "drained_clear") == "1"]
    anom = [r for r in vals if r[pre + "deadlock"] == "0" and r.get(pre + "drained_clear") == "0"]
    late = [r for r in dl if r.get(pre + "dl_time") and float(r[pre + "dl_time"]) > 190.0]
    tp = [float(r[pre + "throughput"]) for r in clear if r.get(pre + "throughput")]
    return dict(n=n, dl=len(dl), late=len(late), mid=len(dl) - len(late),
                anom=len(anom), safe=len(clear),
                tph=statistics.mean(tp) if tp else 0.0)


def coreach():
    p = os.path.join(CO, "coreach_summary.csv")
    if not os.path.exists(p):
        flags.append(f"coreach_summary.csv MISSING")
        return {}
    d = {}
    for r in csv.DictReader(open(p)):
        d[(int(r["N"]), float(r["rate"]))] = r
    return d


print("=" * 100)
print("DRAINED HELD-OUT — per cell, per arm")
print(f"{'cell':<11} {'arm':<11} {'n':>6} {'resid':>7} {'late/mid':>9} {'anom':>5} {'safe':>6} {'tph(clear)':>11}")
print("-" * 100)
cells_seen = 0
for N in GRID_N:
    for rate in GRID_R:
        rows_by_mode = {m: load(m, N, rate) for m in MODES}
        if rows_by_mode["native"] is None:
            flags.append(f"MISSING native cell N{N:02d}/r{rate:.2f}")
            continue
        cells_seen += 1
        # unprotected (native before)
        u = arm_stats(rows_by_mode["native"], "before_")
        if u:
            print(f"N{N:02d}/r{rate:.2f}  {'unprot':<11} {u['n']:>6} {u['dl']:>7} "
                  f"{str(u['late'])+'/'+str(u['mid']):>9} {u['anom']:>5} {u['safe']:>6} {u['tph']:>11.3f}")
            if u['anom']: flags.append(f"N{N:02d}/r{rate:.2f} unprot anomalies={u['anom']}")
        for m in MODES:
            rows = rows_by_mode[m]
            if rows is None:
                flags.append(f"MISSING {m} cell N{N:02d}/r{rate:.2f}"); continue
            a = arm_stats(rows, "after_")
            if not a:
                flags.append(f"N{N:02d}/r{rate:.2f} {m} after arm empty"); continue
            tag = ""
            if a['late']: tag += " <LATE!>"
            if a['anom']: tag += " <ANOM!>"
            print(f"N{N:02d}/r{rate:.2f}  {m:<11} {a['n']:>6} {a['dl']:>7} "
                  f"{str(a['late'])+'/'+str(a['mid']):>9} {a['anom']:>5} {a['safe']:>6} {a['tph']:>11.3f}{tag}")
            if a['late']: flags.append(f"N{N:02d}/r{rate:.2f} {m}: {a['late']} LATE(>190h) protected residual(s)")
            if a['anom']: flags.append(f"N{N:02d}/r{rate:.2f} {m}: {a['anom']} anomaly (dl=0,clear=0)")

print(f"\ncells with native present: {cells_seen}/25")

print("\n" + "=" * 100)
print("COREACH no-op on drained-safe seeds (want 100%)")
print(f"{'cell':<11} {'safe_tested':>12} {'noop':>7} {'frac':>8}  diverge")
print("-" * 100)
co = coreach()
for N in GRID_N:
    for rate in GRID_R:
        r = co.get((N, rate))
        if not r:
            flags.append(f"coreach MISSING N{N:02d}/r{rate:.2f}"); continue
        tested = int(r["safe_tested"]); noop = int(r["noop"])
        dv = r.get("diverge_seeds", "").strip()
        # A cell with 0 safe seeds (high rate / large N -> nothing is unprotected-safe)
        # is not a divergence; only a real no-op miss counts.
        if tested == 0:
            mark = "  (no safe seeds)"
        elif noop < tested:
            mark = "  <-- DIVERGENCE"
            flags.append(f"coreach N{N:02d}/r{rate:.2f}: no-op {noop}/{tested} diverge={dv[:60]}")
        else:
            mark = ""
        fracs = f"{noop/tested:.4f}" if tested else "  n/a"
        print(f"N{N:02d}/r{rate:.2f}  {tested:>12} {noop:>7} {fracs:>8}{mark}")

print("\n" + "=" * 100)
if flags:
    print(f"FLAGS ({len(flags)}) — REVIEW BEFORE TOUCHING PAPER:")
    for f in flags:
        print("  !", f)
else:
    print("NO FLAGS — coreach 100% no-op, no late residuals, no anomalies, all cells present.")
print("=" * 100)
