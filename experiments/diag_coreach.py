#!/usr/bin/env python3
"""Diagnose the coreach r2.00 (and N02/r1.00) divergences.

For a given N/rate/seed that is unprotected-safe yet diverges under the union:
  1. run UNPROTECTED  -> train_log U (must be deadlock-free)
  2. run FULL UNION   -> train_log S ; confirm S != U (binds)
  3. bisect: run each single union constraint alone -> find which bind
  4. dump the culprit constraint rows for inspection

Usage:  python3 diag_coreach.py N rate seed [seed2 ...]
"""
import argparse, os, sys, csv, hashlib, shutil
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import run_heldout_eval as H
from run_experiments import set_csv_option, set_spawn_rate, run_exe

DIAG = os.path.join(SCRIPT_DIR, "work", "diag_coreach")


def _read_groups(path):
    rows = list(csv.reader(open(path, newline="")))
    header = rows[0]
    order, groups = {}, []
    for r in rows[1:]:
        if not r:
            continue
        cid = r[0]
        if cid not in order:
            order[cid] = len(groups); groups.append((cid, []))
        groups[order[cid]][1].append(r)
    return header, groups


def _write_subset(header, groups, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for _cid, grows in groups:
            for r in grows:
                w.writerow(r)


def setup(N, rate):
    wdir = os.path.join(DIAG, f"N{N:02d}", H.rate_label(rate))
    idir = os.path.join(wdir, "input"); os.makedirs(idir, exist_ok=True)
    os.makedirs(os.path.join(wdir, "output"), exist_ok=True)
    base = os.path.join(H.CONFIGS_DIR, f"N{N:02d}", "input")
    for f in os.listdir(base):
        p = os.path.join(base, f)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(idir, f))
    d = os.path.join(idir, "dlexp_options.csv"); r = os.path.join(idir, "runctrl.csv")
    set_csv_option(d, "MAX_ITERATIONS", 1); set_csv_option(d, "CONSTRAINT_EVAL", "tree")
    set_csv_option(d, "START_DEBUG", 0)
    set_csv_option(r, "pl_constraints", 0); set_csv_option(r, "log_output", 2)
    set_spawn_rate(os.path.join(idir, "open_loop_spawners.csv"), rate)
    return wdir


def run(wdir, seed, warm):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("N", type=int); ap.add_argument("rate", type=float)
    ap.add_argument("seeds", type=int, nargs="+")
    args = ap.parse_args()
    N, rate = args.N, args.rate

    # (re)build the per-N union from the freshly trained corridor sets
    upath, nu, used = H.build_union_constraints(N)
    print(f"N{N:02d} union = {nu} constraints, from rates {[f'{r:.2f}' for r in used]}")
    header, ugroups = _read_groups(upath)
    print(f"union file: {upath}")

    wdir = setup(N, rate)
    union_local = os.path.join(wdir, "union.csv")
    shutil.copy2(upath, union_local)

    for seed in args.seeds:
        print(f"\n===== N{N:02d} r{rate:.2f} seed {seed} =====")
        dU, U = run(wdir, seed, "")
        print(f"  unprotected: deadlock={dU}  md5={U}")
        dS, S = run(wdir, seed, "union.csv")
        print(f"  full union : deadlock={dS}  md5={S}  {'NO-OP' if S==U else 'DIVERGES'}")
        if S == U:
            print("  (no divergence for this seed — skipping bisect)")
            continue
        # bisect: each constraint alone
        binders = []
        for cid, grows in ugroups:
            sub = os.path.join(wdir, "one.csv")
            _write_subset(header, [(cid, grows)], sub)
            dO, O = run(wdir, seed, "one.csv")
            tag = "BINDS" if O != U else "no-op"
            dltag = f" DEADLOCK!" if dO == 1 else ""
            print(f"    {cid:>8} alone: {tag}{dltag}  (dl={dO})")
            if O != U:
                binders.append((cid, grows, dO))
        print(f"  --> {len(binders)} single-constraint binder(s): {[b[0] for b in binders]}")
        for cid, grows, dO in binders:
            print(f"\n  --- culprit {cid} (deadlock-when-alone={dO}) rows ---")
            print("      " + ",".join(header))
            for r in grows:
                print("      " + ",".join(r))


if __name__ == "__main__":
    main()
