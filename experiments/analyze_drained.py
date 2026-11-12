#!/usr/bin/env python3
"""Full results check for the drained rerun. Reports per-(N,rate) for each arm:
residual-risk (after_deadlock rate), late(>190h) vs mid split of protected
residuals, throughput over deadlock-free seeds only, safe-seed counts, anomalies;
plus coreach no-op fractions. Emits FLAGS for anything that should halt the
paper update (coreach divergence, late blind-spot residual, anomalies, missing
cells).

The 2026-10-22 campaign runs every stage under both stall policies, so the
results tree gained a policy level that this script predates:

    results/heldout_drained/<policy>/<mode>/N<nn>/r<rate>/heldout_raw.csv
    results/coreach_drained/<policy>/coreach_summary.csv

Until it was taught that level it reported all 45 held-out cells and all 45
coreach cells missing, which is a failure mode worth naming: every cell absent
at once is far likelier to be one wrong path than 45 lost cells.

Default is both arms, discard first because that is the arm the paper reports.
"""
import os, csv, statistics, argparse

RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
POLICIES = ["discard", "bank"]
GRID_N = [2, 5, 10, 15, 20]
GRID_R = [1.00, 1.25, 1.50, 1.75, 2.00, 2.25, 2.50, 2.75, 3.00]
N_CELLS = len(GRID_N) * len(GRID_R)
MODES = ["native", "union", "engineered"]


def load(pol, mode, N, rate):
    p = os.path.join(RES, "heldout_drained", pol, mode,
                     f"N{N:02d}", f"r{rate:.2f}", "heldout_raw.csv")
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


def coreach(pol, flags):
    p = os.path.join(RES, "coreach_drained", pol, "coreach_summary.csv")
    if not os.path.exists(p):
        flags.append("coreach_summary.csv MISSING")
        return {}
    d = {}
    for r in csv.DictReader(open(p)):
        d[(int(r["N"]), float(r["rate"]))] = r
    return d


def report(pol, resid):
    """One policy's full report. Appends per-cell residuals to resid for the
    cross-arm table, and returns this arm's flags."""
    flags = []
    print("=" * 100)
    print(f"DRAINED HELD-OUT, stall policy = {pol} -- per cell, per arm")
    print(f"{'cell':<11} {'arm':<11} {'n':>6} {'resid':>7} {'late/mid':>9} "
          f"{'anom':>5} {'safe':>6} {'tph(clear)':>11}")
    print("-" * 100)
    cells_seen = 0
    for N in GRID_N:
        for rate in GRID_R:
            rows_by_mode = {m: load(pol, m, N, rate) for m in MODES}
            if rows_by_mode["native"] is None:
                flags.append(f"MISSING native cell N{N:02d}/r{rate:.2f}")
                continue
            cells_seen += 1
            # unprotected (native before)
            u = arm_stats(rows_by_mode["native"], "before_")
            if u:
                print(f"N{N:02d}/r{rate:.2f}  {'unprot':<11} {u['n']:>6} {u['dl']:>7} "
                      f"{str(u['late'])+'/'+str(u['mid']):>9} {u['anom']:>5} "
                      f"{u['safe']:>6} {u['tph']:>11.3f}")
                resid[(N, rate, "unprot")][pol] = u
                if u['anom']:
                    flags.append(f"N{N:02d}/r{rate:.2f} unprot anomalies={u['anom']}")
            for m in MODES:
                rows = rows_by_mode[m]
                if rows is None:
                    flags.append(f"MISSING {m} cell N{N:02d}/r{rate:.2f}")
                    continue
                a = arm_stats(rows, "after_")
                if not a:
                    flags.append(f"N{N:02d}/r{rate:.2f} {m} after arm empty")
                    continue
                resid[(N, rate, m)][pol] = a
                tag = ""
                if a['late']: tag += " <LATE!>"
                if a['anom']: tag += " <ANOM!>"
                print(f"N{N:02d}/r{rate:.2f}  {m:<11} {a['n']:>6} {a['dl']:>7} "
                      f"{str(a['late'])+'/'+str(a['mid']):>9} {a['anom']:>5} "
                      f"{a['safe']:>6} {a['tph']:>11.3f}{tag}")
                if a['late']:
                    flags.append(f"N{N:02d}/r{rate:.2f} {m}: {a['late']} "
                                 f"LATE(>190h) protected residual(s)")
                if a['anom']:
                    flags.append(f"N{N:02d}/r{rate:.2f} {m}: {a['anom']} anomaly (dl=0,clear=0)")
    print(f"\ncells with native present: {cells_seen}/{N_CELLS}")
    print("\n" + "=" * 100)
    print(f"COREACH no-op on drained-safe seeds, stall policy = {pol} (want 100%)")
    print(f"{'cell':<11} {'safe_tested':>12} {'noop':>7} {'frac':>8}  diverge")
    print("-" * 100)
    co = coreach(pol, flags)
    for N in GRID_N:
        for rate in GRID_R:
            r = co.get((N, rate))
            if not r:
                flags.append(f"coreach MISSING N{N:02d}/r{rate:.2f}")
                continue
            tested = int(r["safe_tested"]); noop = int(r["noop"])
            dv = r.get("diverge_seeds", "").strip()
            # A cell with 0 safe seeds (high rate / large N -> nothing is unprotected-safe)
            # is not a divergence; only a real no-op miss counts.
            if tested == 0:
                mark = "  (no safe seeds)"
            elif noop < tested:
                mark = "  <-- DIVERGENCE"
                flags.append(f"coreach N{N:02d}/r{rate:.2f}: no-op {noop}/{tested} "
                             f"diverge={dv[:60]}")
            else:
                mark = ""
            fracs = f"{noop/tested:.4f}" if tested else "  n/a"
            print(f"N{N:02d}/r{rate:.2f}  {tested:>12} {noop:>7} {fracs:>8}{mark}")
    return flags


def cross_arm(resid, pols):
    """Residual deadlocks of the two arms side by side, which is the comparison
    the paper reports. Printed only for the modes that go in the body."""
    print("\n" + "=" * 100)
    print("BOTH ARMS, residual deadlocks out of n, " + " vs ".join(pols))
    print(f"{'cell':<11} " + "".join(f"{m:>22}" for m in ("unprot", "native", "engineered")))
    print("-" * 100)
    for N in GRID_N:
        for rate in GRID_R:
            line = f"N{N:02d}/r{rate:.2f} "
            for m in ("unprot", "native", "engineered"):
                cells = []
                for pol in pols:
                    a = resid[(N, rate, m)].get(pol)
                    cells.append("-" if a is None else f"{a['dl']}/{a['n']}")
                line += f"{' vs '.join(cells):>22}"
            print(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", nargs="+", default=POLICIES, choices=POLICIES,
                    help="stall policy arms to report (default: both, discard first)")
    a = ap.parse_args()
    import collections
    resid = collections.defaultdict(dict)
    allflags = []
    for pol in a.policy:
        allflags += [(pol, f) for f in report(pol, resid)]
    if len(a.policy) > 1:
        cross_arm(resid, a.policy)
    print("\n" + "=" * 100)
    if allflags:
        print(f"FLAGS ({len(allflags)}) -- REVIEW BEFORE TOUCHING PAPER:")
        for pol, f in allflags:
            print(f"  ! [{pol}] {f}")
    else:
        print("NO FLAGS -- coreach 100% no-op, no late residuals, no anomalies, "
              "all cells present, in every arm reported.")
    print("=" * 100)


if __name__ == "__main__":
    main()
