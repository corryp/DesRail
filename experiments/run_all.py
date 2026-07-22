#!/usr/bin/env python3
"""
Master runner for the 2026-07-05 bug-fix rerun (orchestration + configs; the
simulator comes from the repo's DesRail/ + x64/, see README_RERUN.md).

Reruns every deadlock-paper experiment on the bug-fixed exe (root-cause SCC
classification now uses self-containment; branch fix/prune-siding-coverage), in
dependency order, then harvests the lean analysis CSVs from work/<set>/ into
results/<set>/. Heavy artifacts (input/, output/, constraints.csv) stay in work/.

Campaigns (ordered; toowoomba_hybrid_benchmark consumes the bsweep's learned set,
so bsweep runs first):
    corridor                     45 cells (5 N x 9 rates), train + benchmark   [paper 4.1]
    overlength                   15 cells (5 N x 3 phi), 4-phase               [paper 4.2]
    toowoomba_bsweep             pure + hybrid, b swept, c=0.50, training-only  [paper 4.3]
    toowoomba_hybrid_benchmark   4x3 throughput grid (PL + learned)            [paper 4.3]
    overlength_ablation          4 stacking cells, prune ON/OFF                [paper ablation]
    pruning_ablation             4 small + 2 big corridor cells, prune ON/OFF  [paper ablation]

Single command (16-core VM, leave 2 free):
    python run_all.py --jobs 14

Other usage:
    python run_all.py --jobs 14 --only corridor overlength
    python run_all.py --jobs 14 --skip toowoomba_bsweep toowoomba_hybrid_benchmark
    python run_all.py --setup-only          # create dirs+configs, don't run
    python run_all.py --harvest-only        # re-harvest results/ from work/

Every campaign runner is independently resumable (reads its own logs in work/ on
start), so run_all.py can be killed and re-run at any point.
"""
import argparse
import os
import shutil
import subprocess
import sys

SET_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNERS_DIR = os.path.join(SET_DIR, "runners")
WORK_ROOT = os.path.join(SET_DIR, "work")
RESULTS_ROOT = os.path.join(SET_DIR, "results")

# Simulator source and exe live in the repo's own DesRail/ and x64/ (single source of
# truth — this bundle carries orchestration and configs only). Walk up to find them;
# an explicit $DESRAIL_REPO still wins, e.g. when the bundle is copied elsewhere.
def _find_repo_root(start):
    d = start
    for _ in range(6):
        if os.path.isfile(os.path.join(d, "DesRail", "makefile")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


if "DESRAIL_REPO" not in os.environ:
    _root = _find_repo_root(SET_DIR)
    if _root is None:
        sys.exit("ERROR: could not locate the DesRail repo root (the dir containing "
                 "DesRail/). Set $DESRAIL_REPO explicitly.")
    os.environ["DESRAIL_REPO"] = _root

# Ordered campaigns: (work-label, script path relative to this folder, extra args).
# Order matters: toowoomba_hybrid_benchmark reads the bsweep's learned set, and the
# two evaluation campaigns read the corridor sets, so they run after training.
CAMPAIGNS = [
    ("corridor", "runners/run_corridor.py", []),
    ("overlength", "runners/run_overlength.py", []),
    ("toowoomba_bsweep", "runners/run_toowoomba_bsweep.py", []),
    ("toowoomba_hybrid_benchmark", "runners/run_toowoomba_hybrid_benchmark.py", []),
    ("overlength_ablation", "runners/run_overlength_ablation.py", []),
    ("pruning_ablation", "runners/run_pruning_ablation.py",
        ["--cells", "N5:r2.50", "N10:r2.50", "N15:r2.50", "N10:r3.00",
         "N15:r2.75", "N20:r2.50"]),
    # --- evaluation of the fixed corridor sets (need work/corridor complete) ---
    ("heldout_eval", "run_heldout_eval.py", ["--mode", "both"]),
    ("coreach_verify", "coreach_verify.py", []),
]

# Campaigns with no config-setup phase (they read configs / prior outputs at run
# time), so they must be skipped under --setup-only.
NO_SETUP_ONLY = {"toowoomba_hybrid_benchmark", "heldout_eval", "coreach_verify"}

# Only these filenames are copied work/ -> results/. Everything else
# (constraints.csv, warm_constraints.csv, input/, output/) stays in work/.
LEAN_FILES = {
    "training_log.csv", "benchmark.csv", "pl_benchmark.csv",
    "hybrid_benchmark.csv", "engineered_benchmark.csv", "learned_benchmark.csv",
    "summary.csv",
}


def harvest(label):
    """Copy lean analysis CSVs from work/<label>/ into results/<label>/."""
    src_root = os.path.join(WORK_ROOT, label)
    dst_root = os.path.join(RESULTS_ROOT, label)
    n_files, n_bytes = 0, 0
    if not os.path.isdir(src_root):
        print(f"  [{label}] nothing to harvest (no work/{label})")
        return 0, 0
    for dirpath, _dirs, files in os.walk(src_root):
        for fname in files:
            if fname not in LEAN_FILES:
                continue
            src = os.path.join(dirpath, fname)
            rel = os.path.relpath(src, src_root)
            dst = os.path.join(dst_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            n_files += 1
            n_bytes += os.path.getsize(src)
    print(f"  [{label}] harvested {n_files} files "
          f"({n_bytes/1024:.0f} KB) -> results/{label}")
    return n_files, n_bytes


def run_campaign(label, script_relpath, extra, jobs, global_passthrough):
    script_abs = os.path.join(SET_DIR, script_relpath)
    cwd = os.path.dirname(script_abs)          # runners/ for runners, else SET_DIR
    cmd = [sys.executable, script_abs, "--jobs", str(jobs)]
    cmd += extra + global_passthrough
    print(f"\n{'='*70}\n=== {label}: {' '.join(cmd)}\n{'='*70}", flush=True)
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"  [{label}] runner exited rc={result.returncode}")
    return result.returncode


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jobs", "-j", type=int, default=14,
                   help="parallel workers passed to each campaign runner (default 14)")
    names = [c[0] for c in CAMPAIGNS]
    p.add_argument("--only", nargs="+", choices=names, metavar="SET",
                   help=f"run only these campaigns (of {names})")
    p.add_argument("--skip", nargs="+", choices=names, metavar="SET",
                   help="skip these campaigns")
    p.add_argument("--setup-only", action="store_true",
                   help="pass --setup-only to each runner (create dirs+configs, don't run)")
    p.add_argument("--harvest-only", action="store_true",
                   help="skip running; just (re)harvest results/ from existing work/")
    args = p.parse_args()

    selected = [c for c in CAMPAIGNS
                if (not args.only or c[0] in args.only)
                and (not args.skip or c[0] not in args.skip)]
    global_passthrough = ["--setup-only"] if args.setup_only else []

    print(f"Bug-fix rerun | set dir: {SET_DIR}")
    print(f"DESRAIL_REPO={os.environ.get('DESRAIL_REPO')}")
    print(f"Campaigns: {[c[0] for c in selected]} | jobs={args.jobs}")

    if args.harvest_only:
        total = 0
        for label, _s, _e in selected:
            total += harvest(label)[1]
        print(f"\nHarvest complete: results/ ~{total/1024/1024:.1f} MB")
        return 0

    for label, script, extra in selected:
        if args.setup_only and label in NO_SETUP_ONLY:
            print(f"\n[{label}] no setup phase (reads configs/prior outputs at run "
                  f"time) — skipped under --setup-only.")
            continue
        rc = run_campaign(label, script, extra, args.jobs, global_passthrough)
        if not args.setup_only:
            harvest(label)
        if rc != 0:
            print(f"WARNING: {label} returned non-zero; continuing to next campaign.")

    if not args.setup_only:
        total = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _d, fs in os.walk(RESULTS_ROOT) for f in fs)
        print(f"\nAll done. results/ = {total/1024/1024:.1f} MB (portable).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
