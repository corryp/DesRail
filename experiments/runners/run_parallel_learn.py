#!/usr/bin/env python3
"""Asynchronous parallel constraint learning for one cell (PROTOTYPE, 2026-10-19).

The sequential learner pins a cell to one core for its whole duration, because
each seed warm-starts from everything learned so far. This runs W workers against
one shared, versioned constraint set instead:

  * every worker picks up the LATEST global set when it starts a seed, and is
    tagged with that set's version;
  * when a worker finishes, the coordinator merges its constraints (always if the
    replication resolved; if it did not, only under --policy bank) and bumps the
    version, then starts the next seed on that worker;
  * the run terminates once R clean replications have accumulated ON ONE
    VERSION. Any merge bumps the version and resets that count.

Why the version tag is not optional
-----------------------------------
A clean replication is evidence only for the exact set it ran under. Adding a
constraint changes trajectories, so a seed that drained under C_t can deadlock
under C_t + {c}: that is the retreating chain of constraint-induced precursors
the paper describes (content.tex:710). A worker that started under C_t and
reports clean after the global set has moved on therefore certifies nothing about
the current set, and its result is recorded as void for the window. Its
constraints are still valid and are still merged, because validity does not
depend on what else is in the set: each cut is coreachability-preserving with
respect to the plant (Theorem 2) and the doomed set is a property of the plant,
fixed (D-invariance, Proposition 2). That is what licenses parallel merging at
all, and no new theory is needed for it.

Reproducibility
---------------
Completion order depends on timing, so the final set is NOT reproducible from the
seed list alone. Instead every merge is journaled with the rows it added, and
`--replay` reconstructs the final set from the journal and checks it matches what
the run shipped. The journal is the artefact, per the repository rule.

Usage
-----
    # prototype a cell, 8 workers, discard policy
    python3 runners/run_parallel_learn.py --N 15 --rate 2.75 --workers 8

    # bank policy, and retest the non-converged seeds at the end
    python3 runners/run_parallel_learn.py --N 15 --rate 2.75 --workers 8 \
        --policy bank --retest

    # verify a finished run reproduces from its journal
    python3 runners/run_parallel_learn.py --N 15 --rate 2.75 --replay

On Aqua, ask for one chunk with ncpus = W (see pbs/cell_parallel.pbs).
"""
import argparse
import csv
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, CAMPAIGN_DIR)
from run_experiments import (  # noqa: E402
    set_csv_option,
    set_spawn_rate,
    apply_drain,
    parse_dlexp_log,
    run_exe,
    rate_label,
    scenario_label,
)

WORK_DIR = os.path.join(CAMPAIGN_DIR, "work", "parallel_learn")
RESULTS_DIR = os.path.join(CAMPAIGN_DIR, "results", "parallel_learn")
CLEAN_THRESHOLD = 100
MAX_SEEDS = 10000
MAX_CONSTRAINTS = 20000
MAX_ITERATIONS_TRAINING = 2000
CONSTRAINT_HEADER = ["constraint_id", "footprint/occupancy_limit",
                     "terminal", "min_length"]


# === the constraint file, as groups ===============================
# One constraint is a contiguous run of rows sharing constraint_id: a header row
# carrying the occupancy limit, then one row per tuple.

def read_groups(path):
    """Read a generated_constraints.csv into [(id, [row, ...]), ...]."""
    if not path or not os.path.exists(path):
        return []
    groups, order = {}, []
    with open(path, newline="") as f:
        rdr = csv.reader(f)
        head = next(rdr, None)
        if head and head[0] != "constraint_id":
            f.seek(0)
            rdr = csv.reader(f)
        for row in rdr:
            if not row or not row[0].strip():
                continue
            cid = row[0]
            if cid not in groups:
                groups[cid] = []
                order.append(cid)
            groups[cid].append(row)
    return [(cid, groups[cid]) for cid in order]


def write_groups(path, groups):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CONSTRAINT_HEADER)
        for _, rows in groups:
            for row in rows:
                w.writerow(row)
    os.replace(tmp, path)       # atomic, so a worker never reads a half-written set


def body_key(rows):
    """Identity of a constraint ignoring its id: the occupancy limit and the set
    of tuples. Two workers that rediscover the same deadlock emit the same body
    under different ids, and this collapses them.

    NOTE a limitation of the prototype: this is exact-match dedup only. The
    engine's merge-and-lower, which replaces a pair of same-geometry constraints
    with the one carrying the lower min_length, runs inside a replication against
    its warm set and so is applied by the next worker to pick the merged set up,
    not by this coordinator. Two concurrent workers can therefore both contribute
    same-geometry constraints differing in min_length, which the sequential path
    would have merged on the spot. Compare final counts between the arms.
    """
    return "|".join(",".join(r[1:]) for r in rows)


def encode_groups(groups):
    """Groups as one journal field: rows "/"-joined, rows within a group joined
    by "+", groups by ";". None of those three characters occurs in a constraint
    row (ids are alphanumeric with underscores, footprints use ":", terminals are
    names, lengths are numeric), so the encoding is lossless and the final set can
    be rebuilt from the journal alone."""
    return ";".join("+".join("/".join(r) for r in rows) for _, rows in groups)


def decode_groups(text):
    groups = []
    for grp in text.split(";"):
        if not grp:
            continue
        rows = [r.split("/") for r in grp.split("+")]
        groups.append((rows[0][0], rows))
    return groups


# === the worker ====================================================

def _worker_dir(sdir, wid):
    return os.path.join(sdir, f"w{wid:02d}")


def prepare_worker(sdir, wid, N, rate):
    """One isolated input/+output/ per worker: the exe resolves ./input/ from its
    working directory, so workers cannot share one."""
    wdir = _worker_dir(sdir, wid)
    input_dir = os.path.join(wdir, "input")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(os.path.join(wdir, "output"), exist_ok=True)
    base_input = os.path.join(CAMPAIGN_DIR, "configs", f"N{N:02d}", "input")
    for fname in os.listdir(base_input):
        src = os.path.join(base_input, fname)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(input_dir, fname))
    set_spawn_rate(os.path.join(input_dir, "open_loop_spawners.csv"), rate)
    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp, "MAX_ITERATIONS", MAX_ITERATIONS_TRAINING)
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "pl_constraints", 0)
    apply_drain(runctrl)        # every arm drains; see A5_DECISION_QUEUE note 6
    return wdir


def run_one_seed(task):
    """Run a single seed in worker `wid`'s directory. Returns a plain dict, so
    nothing but data crosses back to the coordinator."""
    wid, seed, version, snapshot, sdir = task
    wdir = _worker_dir(sdir, wid)
    input_dir = os.path.join(wdir, "input")
    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    runctrl = os.path.join(input_dir, "runctrl.csv")
    warm = os.path.join(wdir, "warm_constraints.csv")

    set_csv_option(runctrl, "seed", seed)
    if snapshot and os.path.exists(snapshot):
        shutil.copy2(snapshot, warm)
        set_csv_option(dlexp, "WARM_START_FILE", "warm_constraints.csv")
        warm_groups = read_groups(warm)
    else:
        if os.path.exists(warm):
            os.remove(warm)
        set_csv_option(dlexp, "WARM_START_FILE", "")
        warm_groups = []

    t0 = time.time()
    err = None
    try:
        run_exe(wdir)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        err = str(e)
    elapsed = time.time() - t0

    log_path = os.path.join(wdir, "output", "dlexp_log.csv")
    rows = parse_dlexp_log(log_path) if os.path.exists(log_path) else []

    # The clean test of A5_DECISION_QUEUE note 6: no deadlock anywhere, no
    # unresolved stall, and (under a drain) the network cleared.
    anomalous = [r for r in rows
                 if not r["deadlock_found"] and not r.get("stalled", 0)
                 and r.get("run_incomplete", 0)]
    dirty = bool(anomalous) or any(
        r["deadlock_found"] or r.get("stalled", 0) for r in rows)
    capped = len(rows) >= MAX_ITERATIONS_TRAINING
    # Resolved = this replication reached a clean run of its own accord, which is
    # how the exe exits; a capped replication never did.
    resolved = bool(rows) and not capped and not rows[-1]["deadlock_found"]

    # What this replication added, as groups, keyed by body so the coordinator can
    # dedup against the global set without trusting the per-run ids (every worker
    # numbers its own constraints from dl_0, so ids collide across workers).
    final_path = os.path.join(wdir, "output", "generated_constraints.csv")
    known = {body_key(r) for _, r in warm_groups}
    added = [(cid, rws) for cid, rws in read_groups(final_path)
             if body_key(rws) not in known]

    return {
        "wid": wid, "seed": seed, "version": version,
        "clean": (not dirty) and bool(rows),
        "resolved": resolved, "capped": capped, "anomalous": len(anomalous),
        "iterations": len(rows),
        "added": added,
        "trains_per_hr": rows[-1]["trains_per_hr"] if rows else 0.0,
        "cpu_time": sum(r["sim_run_time"] for r in rows),
        "elapsed": elapsed,
        "error": err,
    }


# === the coordinator ===============================================

class Coordinator:
    """Holds the one mutable thing in the design. Lives in the parent process, so
    merges are serialised by construction and the version sequence is a total
    order with no locking."""

    def __init__(self, sdir, policy):
        self.sdir = sdir
        self.policy = policy
        self.sets_dir = os.path.join(sdir, "sets")
        os.makedirs(self.sets_dir, exist_ok=True)
        self.groups = []                 # [(id, rows)] in merge order
        self.bodies = set()              # body_key of every group held
        self.version = 0
        self.clean_on_version = 0
        self.snapshot_path = None        # version 0 is the a priori set (empty)
        self.inflight_versions = {}      # version -> count, for snapshot pruning
        self.journal = os.path.join(sdir, "merge_journal.csv")
        self.seed_high = 0
        self.nonconverged = []
        if not os.path.exists(self.journal):
            with open(self.journal, "w", newline="") as f:
                csv.writer(f).writerow(
                    ["event", "seed", "worker", "version_in", "version_out",
                     "clean", "resolved", "capped", "iterations", "n_added",
                     "total_constraints", "clean_on_version", "elapsed", "rows"])
        else:
            self.resume()

    def resume(self):
        """Rebuild state from the journal, so a walltime kill is picked up rather
        than restarted. The journal is append-only and every merge carries the
        rows it added, so the set, the version and the clean count on that version
        all follow from it. Seeds already dispatched are not reissued."""
        events = []
        with open(self.journal, newline="") as f:
            for row in csv.DictReader(f):
                events.append(row)
        for row in events:
            self.seed_high = max(self.seed_high, int(row["seed"]))
            if int(row["capped"]):
                self.nonconverged.append(int(row["seed"]))
            if row["event"] != "merge":
                continue
            self.version += 1
            for cid, rows in decode_groups(row["rows"]):
                self.groups.append((cid, rows))
                self.bodies.add(body_key(rows))
        # Clean runs count only on the version they ran under, so the resumed
        # count is the trailing run of clean events already tagged current.
        for row in reversed(events):
            if row["event"] == "merge":
                break
            if row["event"] == "clean" and int(row["version_in"]) == self.version:
                self.clean_on_version += 1
        if self.version or self.seed_high:
            print(f"  resumed: v{self.version}, {len(self.groups)} constraints, "
                  f"{self.clean_on_version} clean on this version, "
                  f"next seed {self.seed_high + 1}")

    def log(self, event, res, version_out, n_added, rows_text=""):
        with open(self.journal, "a", newline="") as f:
            csv.writer(f).writerow([
                event, res["seed"], res["wid"], res["version"], version_out,
                int(res["clean"]), int(res["resolved"]), int(res["capped"]),
                res["iterations"], n_added, len(self.groups),
                self.clean_on_version, f"{res['elapsed']:.3f}", rows_text])

    def snapshot_for_dispatch(self):
        """The current set, materialised. Versions in flight keep their own file,
        so a merge during a worker's startup cannot change what it picked up."""
        if self.version == 0:
            return None
        path = os.path.join(self.sets_dir, f"v{self.version:06d}.csv")
        if not os.path.exists(path):
            write_groups(path, self.groups)
        self.inflight_versions[self.version] = \
            self.inflight_versions.get(self.version, 0) + 1
        return path

    def release(self, version):
        n = self.inflight_versions.get(version, 0) - 1
        if n > 0:
            self.inflight_versions[version] = n
            return
        self.inflight_versions.pop(version, None)
        if version != self.version:      # keep the current one for new dispatches
            path = os.path.join(self.sets_dir, f"v{version:06d}.csv")
            if os.path.exists(path):
                os.remove(path)

    def accept(self, res):
        """Fold one finished replication in. Returns a short status string."""
        self.release(res["version"])

        if res["error"]:
            self.log("error", res, self.version, 0)
            return f"ERROR {res['error'][:60]}"

        if res["clean"]:
            if res["version"] == self.version:
                self.clean_on_version += 1
                self.log("clean", res, self.version, 0)
                return f"CLEAN {self.clean_on_version}/{CLEAN_THRESHOLD}"
            # Ran under a set we have since moved past: valid work, but it
            # certifies the old version, not this one.
            self.log("clean_void", res, self.version, 0)
            return (f"clean (void: ran on v{res['version']}, "
                    f"now v{self.version})")

        if res["anomalous"]:
            print(f"    WARNING: seed {res['seed']} never cleared the network at "
                  f"the drain horizon and no deadlock was detected. Treated as "
                  f"unclean. This is a bug signature, not an expected outcome.")

        keep = res["resolved"] or self.policy == "bank"
        if not keep:
            self.log("discard", res, self.version, 0)
            return f"not resolved, {len(res['added'])}c DISCARDED"

        fresh = [(cid, rws) for cid, rws in res["added"]
                 if body_key(rws) not in self.bodies]
        if not fresh:
            # Everything it found is already held: another worker got there first.
            self.log("merge_noop", res, self.version, 0)
            return f"{len(res['added'])}c, all already held"

        merged = []
        for cid, rws in fresh:
            # Per-run ids collide across workers, since every replication numbers
            # its own constraints from dl_0, so rename on merge. The seed is in
            # the name, which keeps a constraint traceable to the replication that
            # generated it.
            new_id = f"s{res['seed']}_{cid}"
            merged.append((new_id, [[new_id] + r[1:] for r in rws]))
            self.bodies.add(body_key(rws))
        self.groups.extend(merged)

        self.version += 1
        self.clean_on_version = 0
        rows_text = encode_groups(merged)
        self.log("merge", res, self.version, len(fresh), rows_text)
        write_groups(os.path.join(self.sdir, "constraints.csv"), self.groups)
        return (f"+{len(fresh)}c -> v{self.version} "
                f"({len(self.groups)}c total)")

    def done(self):
        if self.clean_on_version >= CLEAN_THRESHOLD:
            return "converged"
        if len(self.groups) > MAX_CONSTRAINTS:
            return "max_constraints"
        return None


def train(N, rate, W, policy, retest):
    label = scenario_label(N, rate)
    sdir = os.path.join(WORK_DIR, f"N{N:02d}", rate_label(rate), policy)
    os.makedirs(sdir, exist_ok=True)
    print(f"[{label}] parallel learning: W={W}, policy={policy}, "
          f"R={CLEAN_THRESHOLD}")
    for wid in range(W):
        prepare_worker(sdir, wid, N, rate)

    co = Coordinator(sdir, policy)
    nonconverged = list(co.nonconverged)
    seed_next = co.seed_high + 1
    bail = None
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=W) as pool:
        futures = {}            # future -> (wid, seed)
        free = list(range(W))

        def accepting():
            """Still worth starting new seeds?"""
            return (seed_next <= MAX_SEEDS and co.done() is None
                    and bail is None)

        def dispatch():
            nonlocal seed_next
            while free and accepting():
                wid = free.pop()
                snap = co.snapshot_for_dispatch()
                fut = pool.submit(run_one_seed,
                                  (wid, seed_next, co.version, snap, sdir))
                futures[fut] = (wid, seed_next)
                seed_next += 1

        dispatch()
        while futures:
            # Fold in whichever worker finishes first, then immediately give it
            # the next seed against the set as it now stands. Once we stop
            # accepting, the remaining futures still land, so no replication's
            # constraints are thrown away just because the run is ending.
            done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for fut in done:
                wid, seed = futures.pop(fut)
                free.append(wid)
                try:
                    res = fut.result()
                except Exception as e:                      # noqa: BLE001
                    print(f"[{label}] worker {wid} seed {seed} crashed: {e}")
                    bail = f"worker_crash: {e}"
                    continue
                status = co.accept(res)
                if res["capped"]:
                    nonconverged.append(res["seed"])
                tail = "" if accepting() else " [draining]"
                print(f"[{label}] w{wid:02d} seed {res['seed']:5d} "
                      f"(v{res['version']}): {status}{tail}")
            dispatch()

    elapsed = time.time() - t0
    reason = co.done() or bail or ("max_seeds" if seed_next > MAX_SEEDS else "stopped")
    write_groups(os.path.join(sdir, "constraints.csv"), co.groups)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    summary = os.path.join(RESULTS_DIR, "parallel_summary.csv")
    new = not os.path.exists(summary)
    with open(summary, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["N", "rate", "workers", "policy", "reason",
                        "final_constraints", "versions", "seeds_dispatched",
                        "clean_on_final_version", "nonconverged_seeds",
                        "wall_clock_s"])
        w.writerow([N, f"{rate:.2f}", W, policy, reason, len(co.groups),
                    co.version, seed_next - 1, co.clean_on_version,
                    len(nonconverged), f"{elapsed:.1f}"])

    print(f"[{label}] {reason}: {len(co.groups)} constraints over {co.version} "
          f"versions, {seed_next - 1} seeds dispatched, "
          f"{len(nonconverged)} non-converged, {elapsed:.1f}s wall clock")

    if retest and nonconverged:
        retest_seeds(sdir, N, rate, nonconverged, co)
    return reason


def retest_seeds(sdir, N, rate, seeds, co):
    """Out of curiosity, not for the window: run the seeds that never resolved
    against the final set. Their results are NOT folded into the clean count,
    because including seeds chosen for being hard is the data-dependent inclusion
    that invalidates the bound."""
    label = scenario_label(N, rate)
    print(f"[{label}] retesting {len(seeds)} non-converged seeds on the final set")
    snap = os.path.join(sdir, "constraints.csv")
    out = os.path.join(sdir, "nonconverged_retest.csv")
    with open(out, "w", newline="") as f:
        csv.writer(f).writerow(["seed", "clean", "resolved", "capped",
                                "iterations", "elapsed_s"])
    resolved = 0
    for i, seed in enumerate(seeds):
        res = run_one_seed((0, seed, co.version, snap, sdir))
        resolved += int(res["clean"])
        with open(out, "a", newline="") as f:
            csv.writer(f).writerow([seed, int(res["clean"]), int(res["resolved"]),
                                    int(res["capped"]), res["iterations"],
                                    f"{res['elapsed']:.1f}"])
        print(f"[{label}]   seed {seed}: "
              f"{'clean' if res['clean'] else 'still deadlocks'}")
    print(f"[{label}] retest: {resolved}/{len(seeds)} now clean. Reported "
          f"separately; not counted toward the stabilisation window.")


def replay(N, rate, policy):
    """Rebuild the final set from the journal and check it matches. This is the
    reproducibility check the repository rule asks for: regenerating from the
    recorded chain reproduces the artefact exactly."""
    sdir = os.path.join(WORK_DIR, f"N{N:02d}", rate_label(rate), policy)
    journal = os.path.join(sdir, "merge_journal.csv")
    shipped = os.path.join(sdir, "constraints.csv")
    if not (os.path.exists(journal) and os.path.exists(shipped)):
        print(f"nothing to replay in {sdir}")
        return 1
    rebuilt, version = [], 0
    with open(journal, newline="") as f:
        for row in csv.DictReader(f):
            if row["event"] != "merge":
                continue
            version += 1
            for _, rows in decode_groups(row["rows"]):
                rebuilt.extend("/".join(r) for r in rows)
    have = ["/".join(r) for _, rows in read_groups(shipped) for r in rows]
    ok = sorted(rebuilt) == sorted(have)
    print(f"replay: {version} merges, {len(rebuilt)} journaled rows vs "
          f"{len(have)} shipped rows: {'MATCH' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def main():
    global CLEAN_THRESHOLD, MAX_SEEDS
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--N", type=int, required=True)
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--workers", type=int, default=8,
                    help="W. Sized for one Aqua chunk; a larger W queues longer.")
    ap.add_argument("--policy", choices=["discard", "bank"], default="discard",
                    help="what to do with a replication that never resolved")
    ap.add_argument("--clean-threshold", type=int, default=CLEAN_THRESHOLD)
    ap.add_argument("--max-seeds", type=int, default=MAX_SEEDS)
    ap.add_argument("--retest", action="store_true",
                    help="after terminating, rerun the non-converged seeds on "
                         "the final set and report how many resolve")
    ap.add_argument("--replay", action="store_true",
                    help="verify a finished run rebuilds from its journal")
    a = ap.parse_args()

    CLEAN_THRESHOLD = a.clean_threshold
    MAX_SEEDS = a.max_seeds

    if a.replay:
        return replay(a.N, a.rate, a.policy)
    train(a.N, a.rate, a.workers, a.policy, a.retest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
