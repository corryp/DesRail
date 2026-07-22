#!/usr/bin/env python3
"""Coreachability-preservation verification for the (bug-fixed) learned sets.

A sound / self-contained constraint set is coreachability-preserving: it only
blocks transitions INTO doomed states, so on an already-safe trajectory it never
binds. Empirical test: on a seed that is deadlock-free UNPROTECTED, applying the
learned set must produce a byte-identical train_log.csv (a NO-OP).

We test the per-N UNION (the rate-agnostic deployed supervisor, built from the
converged corridor sets) at the low/mid rates where unprotected-safe seeds are
plentiful and where the pre-fix bug over-restricted. Expected: 100% no-op.

Reads the corridor sets produced by the rerun (work/corridor/); run after the
corridor campaign. Writes results/coreach_verify/coreach_summary.csv.

Usage:
    python coreach_verify.py --jobs 8            # default sample
    python coreach_verify.py --sample 100 --rates 1.00 2.00
"""
import argparse, os, sys, subprocess, csv, hashlib
from concurrent.futures import ProcessPoolExecutor, as_completed
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import run_heldout_eval as H
from run_experiments import set_csv_option, set_spawn_rate, run_exe

COREACH_WORK = os.path.join(SCRIPT_DIR, "work", "coreach_verify")
COREACH_RESULTS = os.path.join(SCRIPT_DIR, "results", "coreach_verify")
# Per-N safe-seed sample (union load cost grows with N).
SAMPLE = {2: 300, 5: 300, 10: 200, 15: 120, 20: 100}
RATES = [1.00, 2.00]          # rates with plentiful unprotected-safe seeds


def setup(N):
    """Work dir for N with the per-N union set, verbose logging on."""
    wdir = os.path.join(COREACH_WORK, f"N{N:02d}")
    idir = os.path.join(wdir, "input")
    os.makedirs(idir, exist_ok=True)
    os.makedirs(os.path.join(wdir, "output"), exist_ok=True)
    base = os.path.join(H.CONFIGS_DIR, f"N{N:02d}", "input")
    for f in os.listdir(base):
        if os.path.isfile(os.path.join(base, f)):
            import shutil; shutil.copy2(os.path.join(base, f), os.path.join(idir, f))
    import shutil; shutil.copy2(H.union_path(N), os.path.join(wdir, "union.csv"))
    d = os.path.join(idir, "dlexp_options.csv"); r = os.path.join(idir, "runctrl.csv")
    set_csv_option(d, "MAX_ITERATIONS", 1); set_csv_option(d, "CONSTRAINT_EVAL", "tree")
    set_csv_option(d, "START_DEBUG", 0)          # emit train_log for iteration 0
    set_csv_option(r, "pl_constraints", 0); set_csv_option(r, "log_output", 2)
    return wdir


def _run(wdir, seed, warm):
    idir = os.path.join(wdir, "input")
    set_csv_option(os.path.join(idir, "runctrl.csv"), "seed", seed)
    set_csv_option(os.path.join(idir, "dlexp_options.csv"), "WARM_START_FILE", warm)
    tl = os.path.join(wdir, "output", "train_log.csv")
    if os.path.exists(tl): os.remove(tl)
    run_exe(wdir)
    dl = list(csv.DictReader(open(os.path.join(wdir, "output", "dlexp_log.csv"))))
    deadlock = int(dl[0]["deadlock_found"]) if dl else -1
    md5 = hashlib.md5(open(tl, "rb").read()).hexdigest() if os.path.exists(tl) else None
    return deadlock, md5


def verify_cell(N, rate, sample):
    wdir = setup(N)
    set_spawn_rate(os.path.join(wdir, "input", "open_loop_spawners.csv"), rate)
    safe = noop = 0
    diverge = []
    seed = H.SEED_START
    cap = H.SEED_START + sample * 25          # bound the search for safe seeds
    while safe < sample and seed < cap:
        dU, U = _run(wdir, seed, "")          # unprotected
        if dU == 0 and U is not None:
            safe += 1
            _, S = _run(wdir, seed, "union.csv")
            if S == U: noop += 1
            else: diverge.append(seed)
        seed += 1
    return {"N": N, "rate": rate, "safe": safe, "noop": noop,
            "diverge": diverge, "union_c": _count(H.union_path(N))}


def _count(path):
    ids = set()
    for row in csv.reader(open(path)):
        if row and row[0].startswith("dl_"): ids.add(row[0])
    return len(ids)


def cell_wrapper(args):
    N, rate, sample = args
    try:
        return verify_cell(N, rate, sample)
    except Exception as e:
        print(f"[N{N:02d}/r{rate:.2f}] CRASH: {e}")
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--sample", type=int, default=None, help="override per-cell sample")
    p.add_argument("--rates", nargs="+", type=float, default=RATES)
    args = p.parse_args()

    print("Building per-N union sets from the corridor results...")
    for N in H.GRID_N:
        _, nu, used = H.build_union_constraints(N)
        print(f"  N{N:02d}: {nu} constraints (rates {[f'{r:.2f}' for r in used]})")

    tasks = [(N, r, args.sample or SAMPLE.get(N, 100))
             for N in H.GRID_N for r in args.rates]
    results = []
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            for fut in as_completed({ex.submit(cell_wrapper, t): t for t in tasks}):
                results.append(fut.result())
    else:
        results = [cell_wrapper(t) for t in tasks]
    results = [r for r in results if r]

    os.makedirs(COREACH_RESULTS, exist_ok=True)
    out = os.path.join(COREACH_RESULTS, "coreach_summary.csv")
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["N", "spawn_rate", "union_constraints", "safe_seeds",
                    "noop", "noop_frac", "diverge_seeds"])
        for r in sorted(results, key=lambda x: (x["N"], x["rate"])):
            w.writerow([r["N"], f"{r['rate']:.2f}", r["union_c"], r["safe"], r["noop"],
                        f"{(r['noop']/r['safe']) if r['safe'] else 0:.4f}",
                        " ".join(map(str, r["diverge"][:20]))])
    print(f"\nSummary: {out}")
    allok = True
    for r in sorted(results, key=lambda x: (x["N"], x["rate"])):
        frac = (r["noop"] / r["safe"]) if r["safe"] else 0
        flag = "" if frac == 1.0 else "  <-- OVER-RESTRICTS"
        if frac != 1.0: allok = False
        print(f"  N{r['N']:02d}/r{r['rate']:.2f}  union={r['union_c']:>6}c  "
              f"no-op {r['noop']}/{r['safe']} ({100*frac:.1f}%){flag}")
    print("\nCOREACHABILITY PRESERVED (no-op on all safe seeds): "
          + ("YES" if allok else "NO — see diverge_seeds"))


if __name__ == "__main__":
    main()
