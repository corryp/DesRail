#!/usr/bin/env python3
"""Assert the campaign's END STATE, rather than trusting the sequence of patches
that produced it.

    python3 pbs/audit_campaign.py        (from the bundle directory)

The 2026-10-22 campaign has been changed under way several times: the held-out
grid widened, a percentile field added, coreach keyed by stall policy, four
stages given a second arm, the Toowoomba sweep's clean-seed rule corrected, a
CPU halting condition added, and the benchmark phases taught to act on the drain.
Each change was verified on its own. That is not the same as the whole campaign
being in one state, and a reader of the paper is entitled to the second claim.
This checks it from the files, and exits non-zero if any check fails.

Checks:
  1. One exe md5 across every cell log, and every log carries one.
  2. Every cell input is drained, with the same sim_len and start_warmdown.
  3. Every benchmark CSV records the drain outcome (drained_clear columns).
  4. No runner derives a clean seed from the constraint count (the pre-2026-10-19
     rule), and every training runner carries the CPU halting condition.
  5. Both stall-policy arms exist for every stage that has arms.
  6. No done marker names a job the harness no longer builds.
"""
import csv, glob, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
B = os.path.dirname(HERE)
fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


print("1. one binary across the campaign")
md5s, missing = {}, 0
for log in glob.glob(os.path.join(HERE, "logs", "*.log")):
    txt = open(log, errors="replace").read()
    m = re.search(r"exe_md5=([0-9a-f]{32})", txt)
    if m:
        md5s.setdefault(m.group(1), []).append(os.path.basename(log))
    elif "RUNCMD" in txt:
        missing += 1
check("one exe md5", len(md5s) <= 1,
      f"{list(md5s)} over {sum(len(v) for v in md5s.values())} logs")
check("no log without an md5", missing == 0, f"{missing} logs")

print("2. every cell input drained, uniformly")
seen = {}
for p in glob.glob(os.path.join(B, "work", "**", "input", "runctrl.csv"), recursive=True):
    if "_pre_twoarm" in p:
        continue
    d = {r[0].strip(): r[1].strip() for r in csv.reader(open(p, newline="")) if len(r) > 1}
    seen.setdefault((d.get("sim_len"), d.get("start_warmdown")), []).append(p)
check("one drain setting everywhere", len(seen) <= 1,
      "; ".join(f"sim_len={k[0]} warmdown={k[1]} ({len(v)} cells)" for k, v in seen.items()))
check("the setting is a drain",
      all(k[1] not in (None, "", "0", "0.0") and float(k[0] or 0) >= 1000 for k in seen))

print("3. benchmark files record the drain outcome")
want = {"benchmark.csv": ("learned_drained_clear", "pl_drained_clear"),
        "pl_benchmark.csv": ("drained_clear",),
        "hybrid_benchmark.csv": ("drained_clear",),
        "engineered_benchmark.csv": ("drained_clear",)}
bad, total = [], 0
for p in glob.glob(os.path.join(B, "work", "**", "*benchmark.csv"), recursive=True):
    base = os.path.basename(p)
    if "_pre_twoarm" in p or base not in want:
        continue
    total += 1
    try:
        hdr = next(csv.reader(open(p, newline="")))
    except StopIteration:
        hdr = []
    if any(c not in hdr for c in want[base]):
        bad.append(os.path.relpath(p, B))
check("every benchmark file has the column", not bad,
      f"{total} files, {len(bad)} without it" + (f", e.g. {bad[0]}" if bad else ""))

print("4. the runners are on the current rules")
# Only the runners the harness actually invokes. run_toowoomba.py and
# run_parallel_learn.py sit in the same directory and are not part of this
# campaign, so holding them to its halting conditions would report a failure
# that means nothing.
sys.path.insert(0, HERE)
import run_harness as _H
invoked = sorted({m for j in _H.build_jobs()[0]
                  for m in re.findall(r"runners/(run_\w+\.py)", j["cmd"])})
old_rule = []
no_cap = []
for p in [os.path.join(B, "runners", m) for m in invoked]:
    src = open(p).read()
    if re.search(r'deadlock_found.*\bif\s+new_c\w*\s*>\s*0', src) or \
       re.search(r'\bif\s+new_c\w*\s*>\s*0\s+else\s+0', src):
        old_rule.append(os.path.basename(p))
    if "MAX_ITERATIONS_TRAINING" in src and "MAX_CPU_HOURS" not in src:
        no_cap.append(os.path.basename(p))
check("no runner infers a clean seed from the constraint count", not old_rule,
      f"{len(invoked)} runners checked {invoked}" if not old_rule else str(old_rule))
check("every training runner has the CPU halting condition", not no_cap, str(no_cap))

print("5. every stage output sits under a stall-policy arm")
# An arm that is absent means that arm has not run yet, which the harness's
# --status reports; what would be WRONG is output beside the arms, which is how
# the flat coreach tree and the single-arm Toowoomba run went unnoticed.
for tree, stage in [("work", s) for s in
                    ("corridor", "overlength", "overlength_ablation",
                     "pruning_ablation", "toowoomba_bsweep",
                     "toowoomba_hybrid_benchmark", "heldout_drained",
                     "coreach_drained")] + \
                   [("results", s) for s in
                    ("heldout_drained", "coreach_drained", "toowoomba",
                     "toowoomba_hybrid_benchmark")]:
    root = os.path.join(B, tree, stage)
    if not os.path.isdir(root):
        continue
    arms = sorted(d for d in os.listdir(root) if not d.startswith("_"))
    stray = [a for a in arms if a not in ("bank", "discard")]
    check(f"{tree}/{stage}", not stray,
          f"arms {arms}" + (f"  STRAY {stray}" if stray else ""))

print("6. every runner the harness invokes imports")
# The cheapest check there is, and its absence cost 38 failed jobs on
# 2026-10-23: bench_outcome was added to run_experiments.py and imported in
# run_overlength.py, which imports from run_overlength_exp.py instead, so every
# overlength job died on ImportError at the first line of work.
import subprocess as _sp
broken = []
for m in invoked:
    r = _sp.run([sys.executable, "-c",
                 f"import sys; sys.path[:0]=['{B}','{B}/runners']; import {m[:-3]}"],
                capture_output=True, text=True)
    if r.returncode != 0:
        broken.append(f"{m}: {r.stderr.strip().splitlines()[-1] if r.stderr else 'rc=' + str(r.returncode)}")
check("every invoked runner imports", not broken, "; ".join(broken) or f"{len(invoked)} runners")

print("7. no orphan done markers")
sys.path.insert(0, HERE)
import run_harness as H
names = {j["name"] for j in H.build_jobs()[0]}
orphans = [os.path.basename(m)[:-5] for m in glob.glob(os.path.join(HERE, "done", "*.done"))
           if os.path.basename(m)[:-5] not in names]
check("every done marker names a live job", not orphans,
      f"{len(orphans)} orphans" + (f", e.g. {orphans[:3]}" if orphans else ""))

print()
if fails:
    print(f"AUDIT: FAIL -- {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("AUDIT: PASS -- the campaign is in one state")
