#!/usr/bin/env python3
"""Residual deadlock rate per cell, with exact Clopper-Pearson intervals.

Table 2 of the paper and the per-scenario interval table that supports it. The
residual is the number of the 10,000 held-out replications that deadlock under a
given supervisor, counted from the stage's own raw file:

    results/heldout_drained/<policy>/<mode>/N<nn>/r<rate>/heldout_raw.csv

The supervisor is frozen before the first held-out replication is drawn, so the
sample size is fixed in advance and the Clopper-Pearson interval is exact rather
than asymptotic. Neither this machine nor Aqua has scipy, so the incomplete beta
is computed here by its continued fraction and inverted by bisection. The
self-test checks that against the two cases with a closed form, k = 0 and k = n,
and against a published interval, and it runs on every invocation because a
wrong interval is not visibly wrong.

Staying on the standard library is deliberate: this script is deposited in the
archive's tools/, where it has to run wherever a reader puts it. --check-scipy
runs every cell against scipy.stats.beta.ppf where scipy happens to be
installed, which is how the implementation was validated: 270 cells agreed to
3e-14, or 3e-10 deadlocks per 10,000.

    python3 table_residual.py                      # print both panels
    python3 table_residual.py --out residual.csv   # and write the long form
"""
import os, sys, csv, math, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
POLICIES = ["discard", "bank"]
GRID_N = [2, 5, 10, 15, 20]
GRID_R = [1.00, 1.25, 1.50, 1.75, 2.00, 2.25, 2.50, 2.75, 3.00]
MODES = ["native", "union", "fullunion", "engineered"]
# fullunion exists only where a corridor has a non-converged rate, so under the
# discarding variant only N=20; every other corridor's full union is its
# converged union and is not run twice. Absent cells are skipped, not blank rows.


def _betacf(a, b, x, itmax=300, eps=3e-16):
    """Continued fraction for the incomplete beta, by the modified Lentz method."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    raise RuntimeError(f"betacf did not converge at a={a}, b={b}, x={x}")


def betainc(a, b, x):
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lead = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                    + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return lead * _betacf(a, b, x) / a
    return 1.0 - lead * _betacf(b, a, 1.0 - x) / b


def betainv(p, a, b, tol=1e-14):
    """Inverse of betainc in x, by bisection. Monotone in x, so this is safe."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def clopper_pearson(k, n, alpha=0.05):
    """Exact two-sided interval for a binomial proportion."""
    lo = 0.0 if k == 0 else betainv(alpha / 2.0, k, n - k + 1)
    hi = 1.0 if k == n else betainv(1.0 - alpha / 2.0, k + 1, n - k)
    return lo, hi


def _selftest():
    # k = 0 and k = n have closed forms; the interval must match them exactly.
    for n in (100, 10000):
        lo, hi = clopper_pearson(0, n)
        assert lo == 0.0, lo
        assert abs(hi - (1.0 - 0.025 ** (1.0 / n))) < 1e-12, (hi, n)
        lo, hi = clopper_pearson(n, n)
        assert abs(lo - 0.025 ** (1.0 / n)) < 1e-12, (lo, n)
        assert hi == 1.0, hi
    # a published interval: 1 of 10 is [0.00253, 0.44502]
    lo, hi = clopper_pearson(1, 10)
    assert abs(lo - 0.0025289) < 1e-6, lo
    assert abs(hi - 0.4450161) < 1e-6, hi
    # The paper quotes the Rule of Three for a zero-deadlock scenario, below
    # 3e-4 per replication at n = 10,000. That is the ONE-SIDED 95% bound,
    # 1 - 0.05^(1/n) = 2.996/n, and not the upper end of this two-sided 95%
    # interval, which is 1 - 0.025^(1/n) = 3.69e-4. Both are correct for what
    # they state and they are not interchangeable, so the test pins both.
    one_sided = 1.0 - 0.05 ** (1.0 / 10000)
    assert abs(one_sided - 3.0 / 10000) < 1e-6, one_sided
    _, hi = clopper_pearson(0, 10000)
    assert abs(hi - (1.0 - 0.025 ** (1.0 / 10000))) < 1e-12, hi
    assert hi > one_sided, (hi, one_sided)


def residual(pol, mode, N, rate):
    """(deadlocks, replications) for one cell, or None if the cell is absent."""
    p = os.path.join(RES, "heldout_drained", pol, mode,
                     f"N{N:02d}", f"r{rate:.2f}", "heldout_raw.csv")
    if not os.path.exists(p):
        return None
    k = n = 0
    for r in csv.DictReader(open(p)):
        if r.get("after_deadlock", "") == "":
            continue
        n += 1
        k += int(r["after_deadlock"] == "1")
    return k, n


def safe_seeds(pol, N, rate):
    """Replications that are deadlock-free UNPROTECTED, the sample on which
    coreachability-preservation is tested: a replication that deadlocks without
    a supervisor has no deadlock-free trajectory for one to preserve.

    Read from the native cell's before_ columns, which are the unprotected arm.
    It does not depend on the supervisor, so it is the same in both policy arms,
    and the campaign bears that out to the replication. Clearing the drain is
    required as well as finding no deadlock, matching analyze_drained.py; the
    two differ only on a replication that reports no deadlock yet fails to
    drain, of which the campaign has none.
    """
    p = os.path.join(RES, "heldout_drained", pol, "native",
                     f"N{N:02d}", f"r{rate:.2f}", "heldout_raw.csv")
    if not os.path.exists(p):
        return None
    k = n = 0
    for r in csv.DictReader(open(p)):
        if r.get("before_deadlock", "") == "":
            continue
        n += 1
        k += int(r["before_deadlock"] == "0" and r.get("before_drained_clear") == "1")
    return k, n


def print_safe(pol):
    print("=" * 96)
    print(f"SAFE SEEDS: replications deadlock-free unprotected, stall policy = {pol}")
    print("   N  " + "".join(f"{r:>9.2f}" for r in GRID_R) + f"{'row':>11}")
    pooled = 0
    rows = []
    for N in GRID_N:
        line = f"  {N:>2}  "
        rowtot = 0
        for rate in GRID_R:
            got = safe_seeds(pol, N, rate)
            if got is None:
                line += f"{'-':>9}"
                continue
            k, n = got
            rows.append((N, rate, k, n))
            rowtot += k
            line += f"{k:>9,}"
        pooled += rowtot
        print(line + f"{rowtot:>11,}")
    print(f"  pooled total: {pooled:,}")
    # the paper notes the count need not fall monotonically with lambda
    nonmono = []
    for N in GRID_N:
        seq = [k for (n_, r_, k, _) in rows if n_ == N]
        for i in range(1, len(seq)):
            if seq[i] > seq[i - 1]:
                nonmono.append(f"N{N:02d} rises at r{GRID_R[i]:.2f} "
                               f"({seq[i-1]} to {seq[i]})")
    print("  non-monotone steps: " + ("; ".join(nonmono) if nonmono else "none"))
    return pooled


def main():
    _selftest()
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", nargs="+", default=POLICIES, choices=POLICIES)
    ap.add_argument("--out", help="write the long form, with intervals, here as CSV")
    ap.add_argument("--safe", action="store_true",
                    help="also print the safe-seed grid behind the "
                         "coreachability table")
    ap.add_argument("--check-scipy", action="store_true",
                    help="also compute every interval with scipy and report the "
                         "largest disagreement; skipped where scipy is absent")
    a = ap.parse_args()
    rows = []
    for pol in a.policy:
        for mode in MODES:
            for N in GRID_N:
                for rate in GRID_R:
                    got = residual(pol, mode, N, rate)
                    if got is None:
                        continue
                    k, n = got
                    lo, hi = clopper_pearson(k, n)
                    rows.append([pol, mode, f"N{N:02d}", f"{rate:.2f}", k, n,
                                 f"{lo * 10000:.2f}", f"{hi * 10000:.2f}"])
    if not rows:
        # Saying which directory was searched, because the readers this replaces
        # printed empty tables when the tree moved under them and said nothing.
        print(f"no held-out cells under {os.path.join(RES, 'heldout_drained')}\n"
              f"  expected <policy>/<mode>/N<nn>/r<rate>/heldout_raw.csv "
              f"for policies {POLICIES} and modes {MODES}")
        return 1
    idx = {(r[0], r[1], r[2], r[3]): r for r in rows}
    for pol in a.policy:
        print("=" * 96)
        print(f"RESIDUAL DEADLOCKS per 10,000 held-out replications, stall policy = {pol}")
        for mode in MODES:
            print(f"\n  {mode}")
            print("   N  " + "".join(f"{r:>9.2f}" for r in GRID_R))
            for N in GRID_N:
                line = f"  {N:>2}  "
                for rate in GRID_R:
                    r = idx.get((pol, mode, f"N{N:02d}", f"{rate:.2f}"))
                    line += f"{(r[4] if r else '-'):>9}"
                print(line)
        nat = [r for r in rows if r[0] == pol and r[1] == "native"]
        worst = max(nat, key=lambda r: r[4])
        best = min(nat, key=lambda r: r[4])
        print(f"\n  native range: {best[2]}/r{best[3]} at {best[4]}/{best[5]} "
              f"[{best[6]}, {best[7]}] per 10,000, to "
              f"{worst[2]}/r{worst[3]} at {worst[4]}/{worst[5]} "
              f"[{worst[6]}, {worst[7]}] per 10,000")
        if a.safe:
            print()
            print_safe(pol)
        eng = [r for r in rows if r[0] == pol and r[1] == "engineered"]
        tot_k, tot_n = sum(r[4] for r in eng), sum(r[5] for r in eng)
        print(f"  engineered: {tot_k} deadlocks over {tot_n:,} replications "
              f"in {len(eng)} scenarios")
    if a.check_scipy:
        try:
            from scipy.stats import beta
        except ImportError:
            print("\n  --check-scipy: scipy is not installed here, skipping")
        else:
            worst = 0.0
            for r in rows:
                k, n = r[4], r[5]
                lo = 0.0 if k == 0 else beta.ppf(0.025, k, n - k + 1)
                hi = 1.0 if k == n else beta.ppf(0.975, k + 1, n - k)
                mine_lo, mine_hi = clopper_pearson(k, n)
                worst = max(worst, abs(mine_lo - lo), abs(mine_hi - hi))
            print(f"\n  --check-scipy: {len(rows)} cells, largest disagreement "
                  f"{worst:.3e} ({worst * 10000:.3e} per 10,000)")
    if a.out:
        with open(a.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["policy", "mode", "N", "rate", "deadlocks", "replications",
                        "cp95_lo_per_10k", "cp95_hi_per_10k"])
            w.writerows(rows)
        print(f"\nwrote {len(rows)} rows to {a.out}")


if __name__ == "__main__":
    sys.exit(main() or 0)
