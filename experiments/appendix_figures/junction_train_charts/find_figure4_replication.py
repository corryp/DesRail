#!/usr/bin/env python3
"""Search replications for the before/after situation Figure 4 is built around, and record why one was chosen.

    find_figure4_replication.py --seeds 300 [--b 1.75] [--c 0.75] [--nogood PATH] [--out DIR] [--jobs 1]
    find_figure4_replication.py --score DIR                      # re-score a finished scan

WHY A SEARCH IS NEEDED AT ALL. The published Figure 4 is seed 1 at (lambda_B, lambda_C) = (1.75, 0.75),
run on the July 2026 binary. Commit f672bc0 then changed when a train leaves a speed-limit zone: a rising
limit is now held until the train's tail is clear, where before it applied at the nose. Its own message
records that this changes results on the deadlock-paper datasets, Toowoomba by +13.3 s mean total time.
Trains are uniformly slower, every arrival and every grant decision shifts, and the trajectories the
published panels show cannot be reproduced on the campaign binary at any seed. The situation has to be
found again rather than recovered, which is the same position `experiments/toowoomba/find_illustration.py`
was written for on the DesRail paper, and this script follows it: the replication is chosen, and the
choice is recorded with its reason instead of being a seed someone once used.

WHAT THE FIGURE HAS TO SHOW. Two panels of the same seed, differing only in whether the 5 learned
junction constraints are loaded, so that colour identifies the same physical train across both:

  BEFORE  the junction deadlocks and the network reaches total gridlock inside the plotted window,
          with the flatline wide enough to read and late enough that traffic is first seen flowing.
  AFTER   the same traffic runs the full horizon with no deadlock, holding briefly at the junction
          approach where a constraint defers a conflicting movement.

HOW EACH IS DETECTED. From `train_chart.csv` and `train_metric_log.csv`, which is what the figure plots
and what the delay table for comment 1.m2 is taken from, so the selection reads the run the same way its
reader will. `single_replicate` is used, as the figure runner uses it: there is no DeadlockMonitor to set
max_time at the detection instant, so a gridlocked run ends when the event queue empties and stalled
trains show as flat lines to that wall.

  GRIDLOCK is trains left uncleared: present in the chart, absent from the metric log, which records a
  train only on completion. It is the same test for both panels, so the after panel is held to the
  standard the before panel fails. The gridlock instant is the last movement in the chart.
  A HOLD is a maximal stretch where a train's chainage does not move, taken inside the plotted window.

WHAT IS DELIBERATELY NOT SCORED. How many of the trains present are stalled at the gridlock instant,
which was the first criterion written and is vacuous: `single_replicate` runs until the event queue
empties, so a run that ends early has ended because nothing can move, and everything still present is
stopped by construction. Measuring it also reads low for a reason worth recording, since it cost an hour:
the chart's end-of-run flush emits a final sample for trains the trace is following, and a train blocked
at its entry before it ever took a section is not one of them. On seed 1 that hid 3 of the 14 jammed
trains and made a total stall read as 79%. The jam is therefore sized by what the metric log never
received, which counts those trains, and not by what the chart shows at the wall.

WHAT IT SCORES, and these are the criteria agreed with Paul on 2026-10-23 rather than anything the
simulator has to pass. A replication that scores badly is not a worse simulation, only a worse
illustration:

  * the before panel gridlocks, with the instant inside [DL_MIN, DL_MAX] hours, preferring LATE in that
    band. The first version preferred the middle, on the reasoning that the flatline needs width. That
    was wrong, and rendering the winner showed it: a panel stops at the gridlock instant because nothing
    moves after it, so an early gridlock leaves the rest of the plotted window blank. Seed 150 at 6.34 h
    wasted 3.7 of the 10 hours plotted, while seed 1 at 8.33 h filled the panel and left the dashed line
    near its right edge, which is also where the published figure had it. Width comes from the stalled
    trains, which stop progressively from about 3.5 h, and not from where the last one stops;
  * at least MIN_COMPLETED trains through before it, so "traffic proceeds normally for the first few
    hours" is something the panel actually shows;
  * at least MIN_JAMMED trains left in the jam, so the flatline is populated rather than a line or two;
  * the after panel clearing completely, no train left uncleared;
  * the after panel showing at least MIN_HOLDS holds of at least MIN_HOLD_H inside the window, so the
    mechanism is visible rather than merely absent;
  * as a tie-break, how many of the trains stalled in the before panel are still visibly delayed in the
    after panel, which is the observation the paragraph after the figure is built on.

The drain is applied as `run_heldout_drained.py` applies it, sim_len to a big-M with spawning and the
stats window ending at BASE_SIM_LEN, so this run is drained like everything else in the campaign. Note
what that means for the figure: the plotted window is the network filling and the deadlock forming, while
the delay statistics come from the stationary period after it. The two answer different questions and the
response says so rather than quietly reporting one for the other.

The learned set must be the campaign's own `constraints.csv`. The frozen `tmba_input/nogood_constr.csv`
is in the pre-A5 segment-level format, which `read_generated_constraints` now rejects by name
(`deadlock_avoidance.cpp`), so a run started with it fails loudly instead of running unconstrained.
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, CAMPAIGN_DIR)
from _run_toowoomba_helpers import EXE_PATH, set_csv_option, set_toowoomba_rates  # noqa: E402

TMBA_INPUT = os.path.join(CAMPAIGN_DIR, "tmba_input")
DEFAULT_NOGOOD = os.path.join(CAMPAIGN_DIR, "results", "toowoomba", "discard", "hybrid",
                              "b0.80_c0.50", "constraints.csv")

# Drain, as run_heldout_drained.py sets it.
BASE_SIM_LEN = 192.0
DRAIN_HORIZON = 1000.0
EXE_TIMEOUT = 1800

PLOT_H = 10.0          # the window the published figure plots
DL_MIN, DL_MAX = 4.0, 9.0
MIN_HOLD_H = 0.1       # 6 minutes: a hold shorter than this is not legible at figure scale
MIN_HOLDS = 3
MIN_COMPLETED = 8      # trains through before the gridlock, so free flow is visible first
MIN_JAMMED = 8         # trains left in the jam, so the flatline is populated
TOL_KM = 0.02          # chainage movement below this is "not moving" (find_illustration.py's value)

HDR = ["seed", "verdict", "score", "before_gridlock", "before_dl_h", "before_completed",
       "before_jammed", "after_uncleared", "after_completed", "after_end_h",
       "after_holds", "after_max_hold_h", "caught_trains", "caught_still_delayed", "reject"]


def setup(wdir, mode, seed, b_rate, c_rate, nogood):
    """One run directory, as run_train_charts.py builds it, plus the drain and a seed."""
    input_dir, output_dir = os.path.join(wdir, "input"), os.path.join(wdir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    for fn in os.listdir(TMBA_INPUT):
        s = os.path.join(TMBA_INPUT, fn)
        if os.path.isfile(s) and fn.endswith(".csv"):
            shutil.copy2(s, os.path.join(input_dir, fn))

    # No manual constraints; the engineered passing-loop set comes from the flag.
    with open(os.path.join(input_dir, "signal_constraints.csv"), "w", newline="") as f:
        f.write("str_id,segment / occ_limit,head,target,comment\n")

    # single_replicate auto-loads input/nogood_constr.csv. AFTER gets the campaign's
    # learned set; BEFORE must not have the frozen pre-A5 file sitting there either,
    # since the reader rejects that format outright.
    dst = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(dst):
        os.remove(dst)
    if mode == "after":
        shutil.copy2(nogood, dst)

    set_toowoomba_rates(os.path.join(input_dir, "open_loop_spawners.csv"), b_rate, c_rate)
    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "sim_len", DRAIN_HORIZON)
    rows = list(csv.reader(open(runctrl, newline="")))
    if not any(r and r[0] == "start_warmdown" for r in rows):
        rows.append(["start_warmdown", str(BASE_SIM_LEN), ""])
        with open(runctrl, "w", newline="") as f:
            csv.writer(f).writerows(rows)
    else:
        set_csv_option(runctrl, "start_warmdown", BASE_SIM_LEN)
    set_csv_option(runctrl, "seed", seed)
    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(runctrl, "train_charts", 1)
    set_csv_option(runctrl, "log_output", 1)
    set_csv_option(runctrl, "animate", 0)
    set_csv_option(runctrl, "screen_output", 0)
    set_csv_option(os.path.join(input_dir, "main_option.csv"),
                   "enter experiment type", "single_replicate")
    return output_dir


def run_exe(wdir, exe):
    out = os.path.join(wdir, "output")
    with open(os.path.join(out, "stdout.log"), "w") as o, \
         open(os.path.join(out, "stderr.log"), "w") as e:
        r = subprocess.run([exe], cwd=wdir, stdin=subprocess.DEVNULL,
                           stdout=o, stderr=e, timeout=EXE_TIMEOUT)
    if r.returncode != 0:
        tail = open(os.path.join(out, "stderr.log")).read()[-400:]
        raise RuntimeError(f"rc={r.returncode}: {tail}")


def trajectories(path):
    """train -> (times, chainages) merged across both chainage corridors, time-ordered.

    A junction-crossing train appears on both charts, so per-corridor stretches are kept
    apart: "not moving" has to mean not moving on the corridor the sample belongs to.
    """
    per = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            per.setdefault((r["train"], r["corridor_id"]), []).append(
                (float(r["time"]), float(r["chainage"])))
    for k in per:
        per[k].sort()
    return per


def holds(samples, t_max):
    """Maximal stationary stretches (start, length) inside [0, t_max]."""
    out, i, n = [], 0, len(samples)
    while i < n - 1:
        j = i
        while j + 1 < n and abs(samples[j + 1][1] - samples[i][1]) < TOL_KM:
            j += 1
        if j > i:
            t0, t1 = samples[i][0], samples[j][0]
            if t0 < t_max:
                out.append((t0, min(t1, t_max) - t0))
        i = max(j, i + 1)
    return out


def completed(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="") as f:
        return {r["id"] for r in csv.DictReader(f)}


def measure(wdir):
    """Chart and log facts for one run: what cleared, when it stopped, and where it held."""
    chart = os.path.join(wdir, "output", "train_chart.csv")
    per = trajectories(chart)
    seen = {t for (t, _c) in per}
    done = completed(os.path.join(wdir, "output", "train_metric_log.csv"))
    end = max((s[-1][0] for s in per.values()), default=0.0)
    uncleared = sorted(seen - done)
    hold_list = []
    for (t, _c), s in per.items():
        hold_list += [h for h in holds(s, PLOT_H) if h[1] >= MIN_HOLD_H]
    return dict(end=end, seen=seen, done=done, uncleared=uncleared,
                holds=hold_list, max_hold=max((h[1] for h in hold_list), default=0.0))


def one_seed(args_tuple):
    seed, b_rate, c_rate, nogood, out_dir, exe, keep = args_tuple
    base = os.path.join(out_dir, "work", f"seed_{seed:04d}")
    row = dict.fromkeys(HDR, "")
    row["seed"] = seed
    m = {}
    try:
        for mode in ("before", "after"):
            wdir = os.path.join(base, mode)
            setup(wdir, mode, seed, b_rate, c_rate, nogood)
            run_exe(wdir, exe)
            m[mode] = measure(wdir)
    except Exception as exc:
        row["verdict"] = "ERROR"
        row["reject"] = str(exc)[:200].replace("\n", " ")
        return row

    bef, aft = m["before"], m["after"]
    caught = set(bef["uncleared"])
    # Of the trains stalled in the before panel, how many hold at least 30 minutes
    # somewhere in the after panel's plotted window.
    delayed = 0
    aft_holds_by_train = {}
    for (t, _c), s in trajectories(os.path.join(base, "after", "output", "train_chart.csv")).items():
        hs = [h for h in holds(s, PLOT_H) if h[1] >= 0.5]
        if hs:
            aft_holds_by_train[t] = max(h[1] for h in hs)
    delayed = len(caught & set(aft_holds_by_train))

    row.update(before_gridlock=int(bool(bef["uncleared"])),
               before_dl_h=f"{bef['end']:.4f}",
               before_completed=len(bef["done"]), before_jammed=len(bef["uncleared"]),
               after_uncleared=len(aft["uncleared"]), after_completed=len(aft["done"]),
               after_end_h=f"{aft['end']:.4f}", after_holds=len(aft["holds"]),
               after_max_hold_h=f"{aft['max_hold']:.4f}",
               caught_trains=len(caught), caught_still_delayed=delayed)

    why = []
    if not bef["uncleared"]:
        why.append("before did not gridlock")
    elif not (DL_MIN <= bef["end"] <= DL_MAX):
        why.append(f"gridlock at {bef['end']:.2f} h outside [{DL_MIN}, {DL_MAX}]")
    if len(bef["done"]) < MIN_COMPLETED:
        why.append(f"only {len(bef['done'])} trains through before the gridlock")
    if len(bef["uncleared"]) < MIN_JAMMED:
        why.append(f"only {len(bef['uncleared'])} trains in the jam")
    if aft["uncleared"]:
        why.append(f"after left {len(aft['uncleared'])} trains uncleared")
    if len(aft["holds"]) < MIN_HOLDS:
        why.append(f"only {len(aft['holds'])} legible holds in the window")
    row["verdict"] = "PASS" if not why else "reject"
    row["reject"] = "; ".join(why)

    if not why:
        row["score"] = f"{score_row(row):.2f}"

    if keep == "none" or (keep == "pass" and why):
        shutil.rmtree(base, ignore_errors=True)
    elif keep == "logs":
        # The charts are the bulk: an after panel's is around 5 MB, so 100 seeds is half a
        # gigabyte of shared home for data only the chosen replication's figure needs.
        # fig4_delay_table.py reads the per-train log and the run's own runctrl, and reads
        # a chart only for the one run it reports in full, so an unselected sample for the
        # pooled directional comparison can drop them.
        for mode in ("before", "after"):
            c = os.path.join(base, mode, "output", "train_chart.csv")
            if os.path.exists(c):
                os.remove(c)
    return row


def score_row(row):
    """Score one passing candidate from the facts already measured.

    Kept separate from the measurement so --score can re-rank a finished scan when the
    criteria change, which is how the preference for a late gridlock was applied without
    simulating 300 seeds a second time.
    """
    fill = (float(row["before_dl_h"]) - DL_MIN) / (DL_MAX - DL_MIN)
    return (100 * fill
            + 2 * min(int(row["before_completed"]), 25)
            + 2 * min(int(row["before_jammed"]), 25)
            + 2 * min(int(row["after_holds"]), 10)
            + 3 * int(row["caught_still_delayed"]))


def score_existing(out_dir):
    path = os.path.join(out_dir, "candidates.csv")
    rows = list(csv.DictReader(open(path, newline="")))
    for r in rows:
        if r["verdict"] == "PASS":
            r["score"] = f"{score_row(r):.2f}"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HDR)
        w.writeheader()
        w.writerows(rows)
    report(rows, out_dir)


def report(rows, out_dir):
    pas = [r for r in rows if r["verdict"] == "PASS"]
    err = [r for r in rows if r["verdict"] == "ERROR"]
    pas.sort(key=lambda r: -float(r["score"] or 0))
    print(f"\n{len(rows)} seeds, {len(pas)} pass, {len(err)} errored")
    if err:
        print(f"  first error: seed {err[0]['seed']}: {err[0]['reject']}")
    counts = {}
    for r in rows:
        if r["verdict"] == "reject":
            counts[r["reject"].split(";")[0]] = counts.get(r["reject"].split(";")[0], 0) + 1
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  rejected {v:4d}  {k}")
    if not pas:
        print("\nNo candidate. Widen the band or the rates.")
        return
    print(f"\n{'rank':>4} {'seed':>5} {'score':>7} {'gridlock_h':>10} {'through':>8} "
          f"{'jammed':>7} {'after_cleared':>13} {'holds':>6} {'max_hold_h':>10} "
          f"{'caught/delayed':>14}")
    for i, r in enumerate(pas[:15], 1):
        # Rows come either from this run, where the counts are ints, or from a
        # re-score reading candidates.csv, where they are strings. Format, never add.
        caught = f"{r['caught_still_delayed']}/{r['caught_trains']}"
        print(f"{i:>4} {str(r['seed']):>5} {float(r['score']):>7.2f} "
              f"{float(r['before_dl_h']):>10.3f} "
              f"{str(r['before_completed']):>8} {str(r['before_jammed']):>7} "
              f"{str(r['after_completed']):>13} {str(r['after_holds']):>6} "
              f"{float(r['after_max_hold_h']):>10.3f} {caught:>14}")
    print(f"\nchosen: seed {pas[0]['seed']}, work kept under {out_dir}/work/seed_{int(pas[0]['seed']):04d}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seeds", type=int, default=300)
    p.add_argument("--first-seed", type=int, default=1)
    p.add_argument("--b", type=float, default=1.75)
    p.add_argument("--c", type=float, default=0.75)
    p.add_argument("--nogood", default=DEFAULT_NOGOOD)
    p.add_argument("--out", default=os.path.join(SCRIPT_DIR, "fig4_search"))
    p.add_argument("--exe", default=os.environ.get("DESRAIL_EXE") or EXE_PATH)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--keep", choices=["pass", "all", "logs", "none"], default="pass",
                   help="logs: keep every seed but drop the charts, for a pooled sample")
    p.add_argument("--score", metavar="DIR", help="re-score a finished scan and exit")
    a = p.parse_args()

    if a.score:
        score_existing(a.score)
        return
    if not os.path.isfile(a.exe):
        sys.exit(f"ERROR: exe not found: {a.exe}")
    if not os.path.isfile(a.nogood):
        sys.exit(f"ERROR: learned set not found: {a.nogood}")
    os.makedirs(a.out, exist_ok=True)
    md5 = subprocess.run(["md5sum", a.exe], capture_output=True, text=True).stdout.split()[0]
    with open(os.path.join(a.out, "PROVENANCE.txt"), "w") as f:
        f.write(f"exe            {a.exe}\nexe_md5        {md5}\n"
                f"learned_set    {a.nogood}\nrates          lambda_B={a.b} lambda_C={a.c}\n"
                f"seeds          {a.first_seed}..{a.first_seed + a.seeds - 1}\n"
                f"drain          sim_len={DRAIN_HORIZON} start_warmdown={BASE_SIM_LEN}\n"
                f"band           gridlock in [{DL_MIN}, {DL_MAX}] h, "
                f">= {MIN_COMPLETED} through, >= {MIN_JAMMED} jammed, "
                f">= {MIN_HOLDS} holds >= {MIN_HOLD_H} h inside {PLOT_H} h\n")
    print(open(os.path.join(a.out, "PROVENANCE.txt")).read())

    seeds = range(a.first_seed, a.first_seed + a.seeds)
    work = [(s, a.b, a.c, a.nogood, a.out, a.exe, a.keep) for s in seeds]
    rows = []
    if a.jobs > 1:
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(one_seed, w): w[0] for w in work}
            for fut in as_completed(futs):
                rows.append(fut.result())
                print(f"  seed {rows[-1]['seed']:>5} {rows[-1]['verdict']:<7} "
                      f"{rows[-1]['reject'][:80]}", flush=True)
    else:
        for w in work:
            rows.append(one_seed(w))
            print(f"  seed {rows[-1]['seed']:>5} {rows[-1]['verdict']:<7} "
                  f"{rows[-1]['reject'][:80]}", flush=True)

    rows.sort(key=lambda r: int(r["seed"]))
    with open(os.path.join(a.out, "candidates.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HDR)
        w.writeheader()
        w.writerows(rows)
    report(rows, a.out)


if __name__ == "__main__":
    main()
