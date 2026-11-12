#!/usr/bin/env python3
"""Per-cell PBS harness driver for the bug-fix rerun on Aqua (PBS Pro).

Runs ON the HPC login node. Enumerates every experiment cell as a single-threaded
job (`pbs/cell.pbs`, ncpus=1, 48h) and submits any cell that is (a) not already
DONE (no `pbs/done/<job>.done` marker), (b) not already queued/running, and
(c) has its dependencies satisfied. Re-running the driver therefore both submits
new work and RESUBMITS cells killed by the 48h wall-time (they resume from their
own training_log/warm_constraints).

Dependencies (marker-based, checked here — no PBS -W needed):
  - toowoomba_hybrid_benchmark  needs  toowoomba bsweep hybrid b0.50
  - heldout_eval / coreach_verify  need  ALL corridor cells done

Usage (on the login node, from the bugfix_rerun folder):
  python pbs/run_harness.py --dry-run     # show what would be submitted
  python pbs/run_harness.py               # submit ready cells
  python pbs/run_harness.py --status      # progress summary
  python pbs/run_harness.py --only corridor overlength
  # cron/loop:  watch -n 600 'python pbs/run_harness.py'   (or a --loop wrapper)
"""
import argparse, glob, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # the bugfix_rerun folder
DONE = os.path.join(HERE, "done")
FAILED = os.path.join(HERE, "failed")            # jobs we've given up on (need a human)
ATTEMPTS = os.path.join(HERE, "attempts")        # per-job submit counter
PBS_TEMPLATE = os.path.join(HERE, "cell.pbs")
MAX_ATTEMPTS = 8                                 # backstop for jobs killed without progress

CORR_N = [2, 5, 10, 15, 20]
CORR_R = [1.00, 1.25, 1.50, 1.75, 2.00, 2.25, 2.50, 2.75, 3.00]
HOUT_R = CORR_R   # held-out eval grid: every trained rate, see the note below
OVL_N = [2, 5, 10, 15, 20]
OVL_F = [0.10, 0.25, 0.50]
BSWEEP_B = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
OVLABL = [(15, 0.10), (20, 0.10), (20, 0.25), (10, 0.25)]
PRABL = [(5, 2.50), (10, 2.50), (15, 2.50), (10, 3.00), (15, 2.75), (20, 2.50)]

# The held-out grid was five of the nine trained rates (1.00, 2.00, 2.50, 2.75,
# 3.00) until 2026-10-22, which is where the submitted paper's "250,000 runs over
# 25 scenarios" comes from. Reviewer comment 1.4 asks for the complete
# scenario-level results, so every trained rate is now evaluated: 45 scenarios
# per stall policy rather than 25. The widening is affordable because the four
# added rates carry few constraints (at most 350 at any N in the submitted
# table) and per-run time tracks constraint count. Measured against the 48h
# wall, the slowest 1,000-seed slice in the previous campaign was N20/r3.00
# native at 4.3 hours, so the slices are left at ten per cell.


# Both stall policies are trained and compared, so every training cell and every
# evaluation of it exists twice (decision queue, rerun note 5). The two arms must
# stay separate the whole way through and must never be unioned, since comparing
# them is the point. The policy is a level in the work and results trees and part
# of each job name, and it reaches the scripts two ways: the training runners take
# --policy, while the evaluation scripts fix their paths at import and so need
# STALL_POLICY in the environment.
POLICIES = ("bank", "discard")


def R(runner, policy=None):
    cmd = f"python runners/{runner}"
    return cmd if policy is None else f"{cmd} --policy {policy}"


def E(script, policy):
    """An evaluation command, with the policy exported before the script starts."""
    return f"STALL_POLICY={policy} python {script}"


def build_jobs():
    """Return list of dicts: name, cmd, campaign, deps (list of job names or
    the sentinel 'CORRIDOR_ALL')."""
    jobs = []
    corr_by_policy = {pol: [] for pol in POLICIES}
    for pol in POLICIES:
        for n in CORR_N:
            for r in CORR_R:
                nm = f"dl_corr_{pol}_N{n:02d}r{r:.2f}"
                corr_by_policy[pol].append(nm)
                jobs.append(dict(name=nm, campaign="corridor", deps=[],
                                 cmd=R(f"run_corridor.py --scenarios N{n:02d}:r{r:.2f} --jobs 1", pol)))
        for n in OVL_N:
            for f in OVL_F:
                jobs.append(dict(name=f"dl_ovl_{pol}_N{n:02d}f{f:.2f}", campaign="overlength", deps=[],
                                 cmd=R(f"run_overlength.py --scenarios N{n:02d}:f{f:.2f} --jobs 1", pol)))
        # --- Benchmark redo, 2026-10-23. The benchmark phases recorded only the
        # monitor's deadlock flag until that date, dropping the unresolved-stall
        # and drain-tripwire outcomes the exe reported in the same row, which
        # under Paul's rule are the same outcome. Each job is gated on its own
        # cell, so a cell still training is never disturbed, and the clear step
        # removes only a file whose header lacks the new column, so a
        # resubmitted job repeats nothing. Training resumes as already converged
        # or already bailed, so the second run is the benchmark seeds alone.
        # NB no --skip-training for overlength: run_scenario() skips the hybrid
        # benchmark on that flag, which is one of the three phases being redone.
        for n in CORR_N:
            for r in CORR_R:
                cell = f"work/corridor/{pol}/N{n:02d}/r{r:.2f}"
                jobs.append(dict(name=f"dl_cbench_{pol}_N{n:02d}r{r:.2f}",
                                 campaign="corridor_bench",
                                 deps=[f"dl_corr_{pol}_N{n:02d}r{r:.2f}"],
                                 cmd=f"python3 pbs/clear_stale_benchmarks.py {cell} && "
                                     + R(f"run_corridor.py --scenarios N{n:02d}:r{r:.2f} --jobs 1", pol)))
        for n in OVL_N:
            for f in OVL_F:
                cell = f"work/overlength/{pol}/N{n:02d}/f{f:.2f}"
                jobs.append(dict(name=f"dl_obench_{pol}_N{n:02d}f{f:.2f}",
                                 campaign="overlength_bench",
                                 deps=[f"dl_ovl_{pol}_N{n:02d}f{f:.2f}"],
                                 cmd=f"python3 pbs/clear_stale_benchmarks.py {cell} && "
                                     + R(f"run_overlength.py --scenarios N{n:02d}:f{f:.2f} --jobs 1", pol)))
    # Toowoomba and the two ablations joined the two-arm structure on 2026-10-23,
    # so the policy is part of their job names and their cell paths, as it
    # already was for corridor and overlength. Each policy's hybrid benchmark
    # waits on that policy's hybrid b0.50 sweep cell, because the set it
    # benchmarks is the one that cell learned. The single-arm done markers these
    # names supersede are archived by pbs/archive_single_arm_markers.sh.
    for pol in POLICIES:
        hyb050 = None
        for mode in ("pure", "hybrid"):
            for b in BSWEEP_B:
                nm = f"dl_tmba_{pol}_{mode}_b{b:.2f}"
                if mode == "hybrid" and abs(b - 0.50) < 1e-9:
                    hyb050 = nm
                jobs.append(dict(name=nm, campaign="toowoomba_bsweep", deps=[],
                                 cmd=R(f"run_toowoomba_bsweep.py --modes {mode} --bvals {b:.2f} --jobs 1", pol)))
        jobs.append(dict(name=f"dl_tmba_{pol}_hybbench", campaign="toowoomba_hybrid_benchmark",
                         deps=[hyb050],
                         cmd=R("run_toowoomba_hybrid_benchmark.py --jobs 1", pol)))
        for n, f in OVLABL:
            jobs.append(dict(name=f"dl_ovlabl_{pol}_N{n:02d}f{f:.2f}", campaign="overlength_ablation", deps=[],
                             cmd=R(f"run_overlength_ablation.py --cells N{n:02d}:f{f:.2f} --jobs 1", pol)))
        for n, r in PRABL:
            jobs.append(dict(name=f"dl_prabl_{pol}_N{n:02d}r{r:.2f}", campaign="pruning_ablation", deps=[],
                             cmd=R(f"run_pruning_ablation.py --cells N{n:02d}:r{r:.2f} --jobs 1", pol)))
    # NB: everything below is DRAINED (see apply_drain): training campaigns above
    # already drain via their runners; the eval below uses the drained runners.
    # The old non-drained run_heldout_eval.py / coreach_verify.py are retired.

    # --- DRAINED held-out (fair safe-seed oracle): 10k seeds/cell, sliced into
    # 10 x 1000 so each PBS job finishes well inside walltime. Each slice is an
    # isolated unit; a per-cell merge job (gated on its 10 slices) concatenates
    # the parts, and a per-(N,rate) safe job emits the safe-seed list Phase C
    # (coreach) consumes. run_heldout_drained.py lives at ROOT (no R() helper).
    DRAIN_SLICES = 10
    for pol in POLICIES:
        for n in CORR_N:
            for r in HOUT_R:
                # native = learned per-cell set (has the unprotected before arm ->
                # safe list); union = rate-agnostic learned set; engineered = the
                # passing-loop baseline on the SAME held-out seeds (after-only).
                # A policy's evaluation waits only on that policy's training, so
                # one arm finishing late does not hold the other up.
                for mode in ("native", "union", "engineered"):
                    slice_names = []
                    for s in range(DRAIN_SLICES):
                        nm = f"dl_hdr_{pol}_{mode}_N{n:02d}r{r:.2f}_s{s:02d}"
                        slice_names.append(nm)
                        jobs.append(dict(name=nm, campaign="heldout_drained",
                                         deps=[f"CORRIDOR_ALL:{pol}"],
                                         cmd=E(f"run_heldout_drained.py cell --N {n} --rate {r:.2f} --mode {mode} --slice {s}", pol)))
                    jobs.append(dict(name=f"dl_hdr_{pol}_{mode}_N{n:02d}r{r:.2f}_merge", campaign="heldout_drained",
                                     deps=slice_names,
                                     cmd=E(f"run_heldout_drained.py merge --N {n} --rate {r:.2f} --mode {mode}", pol)))
                jobs.append(dict(name=f"dl_hdr_{pol}_safe_N{n:02d}r{r:.2f}", campaign="heldout_drained",
                                 deps=[f"dl_hdr_{pol}_native_N{n:02d}r{r:.2f}_merge"],
                                 cmd=E(f"run_heldout_drained.py merge --N {n} --rate {r:.2f} --mode safe", pol)))

    # --- DRAINED coreach (Phase C): no-op check of the per-N union on EVERY
    # drained-safe seed, all rates. Each cell's slices depend on that cell's
    # safe-list job; a merge job aggregates the no-op counts. Slices beyond the
    # safe-list length are no-ops (empty part).
    for pol in POLICIES:
        for n in CORR_N:
            for r in HOUT_R:
                co_slices = []
                for s in range(DRAIN_SLICES):
                    nm = f"dl_cod_{pol}_N{n:02d}r{r:.2f}_s{s:02d}"
                    co_slices.append(nm)
                    jobs.append(dict(name=nm, campaign="coreach_drained",
                                     deps=[f"dl_hdr_{pol}_safe_N{n:02d}r{r:.2f}"],
                                     cmd=E(f"run_coreach_drained.py cell --N {n} --rate {r:.2f} --slice {s}", pol)))
                jobs.append(dict(name=f"dl_cod_{pol}_N{n:02d}r{r:.2f}_merge", campaign="coreach_drained",
                                 deps=co_slices,
                                 cmd=E(f"run_coreach_drained.py merge --N {n} --rate {r:.2f}", pol)))
    return jobs, corr_by_policy


def is_done(name):
    return os.path.exists(os.path.join(DONE, name + ".done"))


def is_failed(name):
    return os.path.exists(os.path.join(FAILED, name + ".failed"))


def mark_failed(name, reason):
    os.makedirs(FAILED, exist_ok=True)
    with open(os.path.join(FAILED, name + ".failed"), "w") as f:
        f.write(reason + "\n")
    print(f"  GIVE UP {name}: {reason} (clear pbs/failed/{name}.failed to retry)")


def n_attempts(name):
    try:
        return int(open(os.path.join(ATTEMPTS, name)).read().strip())
    except Exception:
        return 0


def bump_attempts(name):
    os.makedirs(ATTEMPTS, exist_ok=True)
    with open(os.path.join(ATTEMPTS, name), "w") as f:
        f.write(str(n_attempts(name) + 1))


def errored_clean(name):
    """True iff the job's latest log shows a clean non-zero exit (the runner ran
    and returned an error) -- distinct from a wall-time kill, which leaves no
    'done rc=' line. A clean error will just recur, so we must NOT loop-resubmit."""
    logs = sorted(glob.glob(os.path.join(HERE, "logs", name + ".*.log")),
                  key=os.path.getmtime)
    if not logs:
        return False
    try:
        rcs = re.findall(r"\] done rc=(\d+)", open(logs[-1]).read())
    except OSError:
        return False
    return bool(rcs) and int(rcs[-1]) != 0


def queued_names():
    """Job names currently in the PBS queue (Q/R/E/H), by -N name."""
    try:
        out = subprocess.run(["qstat", "-f"], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return set()
    names = set()
    for blk in out.split("Job Id:"):
        nm = None
        for line in blk.splitlines():
            line = line.strip()
            if line.startswith("Job_Name ="):
                nm = line.split("=", 1)[1].strip()
        if nm:
            names.add(nm)
    return names


def deps_met(job, corr_by_policy):
    for d in job["deps"]:
        if d and d.startswith("CORRIDOR_ALL:"):
            pol = d.split(":", 1)[1]
            if not all(is_done(c) for c in corr_by_policy[pol]):
                return False
        elif d and not is_done(d):
            return False
    return True


def submit(job):
    cmd = ["qsub", "-N", job["name"], "-v", f"RUNCMD={job['cmd']}", PBS_TEMPLATE]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print(f"  qsub FAILED {job['name']}: {r.stderr.strip()}")
        return None
    return r.stdout.strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--only", nargs="+", help="restrict to these campaigns")
    p.add_argument("--limit", type=int, default=0, help="submit at most N (0 = no limit)")
    args = p.parse_args()
    os.makedirs(DONE, exist_ok=True)
    jobs, corr_by_policy = build_jobs()
    if args.only:
        jobs = [j for j in jobs if j["campaign"] in args.only]

    if args.status:
        from collections import Counter
        tot, don, fail = Counter(), Counter(), Counter()
        for j in jobs:
            tot[j["campaign"]] += 1
            if is_done(j["name"]):
                don[j["campaign"]] += 1
            elif is_failed(j["name"]):
                fail[j["campaign"]] += 1
        q = queued_names()
        print(f"{'campaign':<28} done/total  queued  FAILED")
        for c in dict.fromkeys(j["campaign"] for j in jobs):
            nq = sum(1 for j in jobs if j["campaign"] == c and j["name"] in q)
            print(f"{c:<28} {don[c]:>4}/{tot[c]:<4}  {nq:>6}  {fail[c]:>6}")
        print(f"TOTAL {sum(don.values())}/{len(jobs)} done, {sum(fail.values())} FAILED, "
              f"{len(q & {j['name'] for j in jobs})} in queue")
        return 0

    q = queued_names()
    n_sub = n_done = n_q = n_dep = n_fail = 0
    for j in jobs:
        nm = j["name"]
        if is_done(nm):
            n_done += 1; continue
        if is_failed(nm):
            n_fail += 1; continue
        if nm in q:
            n_q += 1; continue
        # A clean non-zero exit means the runner ran and errored -- it will just
        # recur, so give up rather than loop-resubmit (this is what caused the
        # 2000+ no-op resubmissions when the eval path was wrong).
        if errored_clean(nm):
            mark_failed(nm, "runner exited non-zero (see pbs/logs)"); n_fail += 1; continue
        if n_attempts(nm) >= MAX_ATTEMPTS:
            mark_failed(nm, f"{MAX_ATTEMPTS} attempts without completion"); n_fail += 1; continue
        if not deps_met(j, corr_by_policy):
            n_dep += 1; continue
        if args.limit and n_sub >= args.limit:
            break
        if args.dry_run:
            print(f"  WOULD SUBMIT {nm}: {j['cmd']}")
        else:
            jid = submit(j)
            if jid:
                bump_attempts(nm)
                print(f"  submitted {nm} -> {jid}")
        n_sub += 1
    verb = "would submit" if args.dry_run else "submitted"
    print(f"\n{verb} {n_sub} | done {n_done} | in queue {n_q} | waiting on deps {n_dep} "
          f"| FAILED {n_fail} | total {len(jobs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
