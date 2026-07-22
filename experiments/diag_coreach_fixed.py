#!/usr/bin/env python3
"""Corrected coreachability no-op check.

Root cause of the apparent r2.00 divergences: the "unprotected-safe" oracle used
deadlock_found==0 at sim_len=192, but the deadlock detector needs
DEADLOCK_TIMEOUT (=2h) of no-progress to declare a deadlock. A gridlock that
forms in the last 2h of the run is never detected -> the seed is MISLABELED safe,
and the (correct) constraint legitimately binds to prevent it, which the old
check scored as a coreachability "divergence".

Fix: certify safety at an EXTENDED horizon (base + 2*DEADLOCK_TIMEOUT margin) so
late gridlocks are detected, then compare the protected vs unprotected logs over
the BASE window. On a truly-safe seed the constraint must be a byte-identical
no-op. Expected: 100%.

Usage: python3 diag_coreach_fixed.py N rate [--sample 300] [--base 192] [--gate 200]
"""
import argparse, os, sys, csv, hashlib, shutil
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import run_heldout_eval as H
from run_experiments import set_csv_option, set_spawn_rate, run_exe

DIAG = os.path.join(SCRIPT_DIR, "work", "diag_coreach")


def setup(N, rate):
    wdir = os.path.join(DIAG, f"N{N:02d}", H.rate_label(rate))
    idir = os.path.join(wdir, "input"); os.makedirs(idir, exist_ok=True)
    os.makedirs(os.path.join(wdir, "output"), exist_ok=True)
    base = os.path.join(H.CONFIGS_DIR, f"N{N:02d}", "input")
    for f in os.listdir(base):
        p = os.path.join(base, f)
        if os.path.isfile(p): shutil.copy2(p, os.path.join(idir, f))
    d = os.path.join(idir, "dlexp_options.csv"); r = os.path.join(idir, "runctrl.csv")
    set_csv_option(d, "MAX_ITERATIONS", 1); set_csv_option(d, "CONSTRAINT_EVAL", "tree")
    set_csv_option(d, "START_DEBUG", 0)
    set_csv_option(r, "pl_constraints", 0); set_csv_option(r, "log_output", 2)
    set_spawn_rate(os.path.join(idir, "open_loop_spawners.csv"), rate)
    shutil.copy2(H.union_path(N), os.path.join(wdir, "union.csv"))
    return wdir


def run(wdir, seed, warm, sim_len, trunc_at=None):
    idir = os.path.join(wdir, "input")
    set_csv_option(os.path.join(idir, "runctrl.csv"), "seed", seed)
    set_csv_option(os.path.join(idir, "runctrl.csv"), "sim_len", sim_len)
    set_csv_option(os.path.join(idir, "dlexp_options.csv"), "WARM_START_FILE", warm)
    tl = os.path.join(wdir, "output", "train_log.csv")
    if os.path.exists(tl): os.remove(tl)
    run_exe(wdir)
    dl = list(csv.DictReader(open(os.path.join(wdir, "output", "dlexp_log.csv"))))
    deadlock = int(dl[0]["deadlock_found"]) if dl else -1
    # md5 over rows with time <= trunc_at (compare only the base window)
    md5 = None
    if os.path.exists(tl):
        h = hashlib.md5()
        with open(tl, "rb") as f:
            for line in f:
                if trunc_at is not None:
                    try:
                        t = float(line.split(b",", 1)[0])
                        if t > trunc_at: continue
                    except ValueError:
                        pass  # header
                h.update(line)
        md5 = h.hexdigest()
    return deadlock, md5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("N", type=int); ap.add_argument("rate", type=float)
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--base", type=float, default=192.0)
    ap.add_argument("--gate", type=float, default=200.0)
    args = ap.parse_args()
    N, rate, base, gate = args.N, args.rate, args.base, args.gate
    wdir = setup(N, rate)

    safe = noop = mislabeled = 0
    diverge_and_doomed = []
    diverge_but_safe = []
    seed = H.SEED_START
    cap = H.SEED_START + args.sample * 25
    while safe < args.sample and seed < cap:
        # base-window unprotected trajectory
        dU_base, U = run(wdir, seed, "", base, trunc_at=base)
        if dU_base != 0 or U is None:
            seed += 1; continue
        # old oracle would call this "safe". Re-gate at extended horizon:
        dU_gate, _ = run(wdir, seed, "", gate, trunc_at=None)
        if dU_gate != 0:
            mislabeled += 1                     # doomed, just detected late
            # confirm the constraint would have bound (divergence) on this seed
            _, S = run(wdir, seed, "union.csv", base, trunc_at=base)
            if S != U: diverge_and_doomed.append(seed)
            seed += 1; continue
        # truly safe over [0, gate]: constraint must be a no-op over [0, base]
        safe += 1
        _, S = run(wdir, seed, "union.csv", base, trunc_at=base)
        if S == U: noop += 1
        else: diverge_but_safe.append(seed)
        seed += 1

    print(f"\n=== N{N:02d} r{rate:.2f}  (base={base}h gate={gate}h) ===")
    print(f"truly-safe seeds (deadlock-free at gate horizon): {safe}")
    print(f"  no-op (byte-identical over base window)        : {noop}  ({100*noop/safe if safe else 0:.2f}%)")
    print(f"  divergence-on-truly-safe (REAL violations)     : {len(diverge_but_safe)}  {diverge_but_safe[:20]}")
    print(f"mislabeled-safe seeds (late deadlock, excluded)  : {mislabeled}")
    print(f"  of which the union correctly bound (was 'diverge' in old check): {len(diverge_and_doomed)}  {diverge_and_doomed[:20]}")


if __name__ == "__main__":
    main()
