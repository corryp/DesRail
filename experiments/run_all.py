#!/usr/bin/env python3
"""
Master runner for the 2026-06-02 full-backer-set pruning rerun.

Runs each campaign runner (corridor -> overlength -> toowoomba ->
pruning_ablation) in sequence with a shared --jobs setting, then harvests ONLY
the lean analysis CSVs from work/<set>/ into results/<set>/. Heavy artifacts
(input/, output/, constraints.csv, warm_constraints.csv) stay under work/ on
the VM and are never copied into results/, so results/ is small and portable.

Usage:
    python run_all.py --jobs 12                       # full sequence
    python run_all.py --jobs 8 --only corridor overlength
    python run_all.py --jobs 12 --skip toowoomba
    python run_all.py --setup-only                    # create dirs+configs only
    python run_all.py --harvest-only                  # re-harvest results/ from work/

Each campaign runner is independently resumable (it reads its own
training_log.csv from work/ on start), so run_all.py can be killed and re-run.
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

# Ordered campaigns: (label, runner script). Order = cheapest/fastest first.
CAMPAIGNS = [
    ("corridor", "run_corridor.py"),
    ("overlength", "run_overlength.py"),
    ("toowoomba", "run_toowoomba.py"),
    ("pruning_ablation", "run_pruning_ablation.py"),
]

# Only these filenames are copied work/ -> results/. Everything else
# (constraints.csv, warm_constraints.csv, input/, output/) stays in work/.
LEAN_FILES = {
    "training_log.csv",
    "benchmark.csv",
    "pl_benchmark.csv",
    "hybrid_benchmark.csv",
    "engineered_benchmark.csv",
    "learned_benchmark.csv",
    "summary.csv",
}


def harvest(label):
    """Copy lean analysis CSVs from work/<label>/ into results/<label>/,
    preserving the scenario subpath. Returns (files, bytes) copied."""
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


def run_campaign(label, script, jobs, passthrough):
    cmd = [sys.executable, os.path.join(RUNNERS_DIR, script), "--jobs", str(jobs)]
    cmd += passthrough
    print(f"\n{'='*70}\n=== {label}: {' '.join(cmd)}\n{'='*70}")
    result = subprocess.run(cmd, cwd=RUNNERS_DIR)
    if result.returncode != 0:
        print(f"  [{label}] runner exited rc={result.returncode}")
    return result.returncode


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jobs", "-j", type=int, default=12,
                   help="parallel workers passed to each campaign runner")
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

    passthrough = ["--setup-only"] if args.setup_only else []

    print(f"Full-backer-set rerun | set dir: {SET_DIR}")
    print(f"Campaigns: {[c[0] for c in selected]} | jobs={args.jobs}")

    if args.harvest_only:
        total = 0
        for label, _ in selected:
            total += harvest(label)[1]
        print(f"\nHarvest complete: results/ ~{total/1024/1024:.1f} MB")
        return 0

    for label, script in selected:
        rc = run_campaign(label, script, args.jobs, passthrough)
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
