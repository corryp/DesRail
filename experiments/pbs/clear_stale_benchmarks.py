#!/usr/bin/env python3
"""Delete benchmark CSVs written before the benchmark phases acted on the drain.

    python3 pbs/clear_stale_benchmarks.py                      # report only
    python3 pbs/clear_stale_benchmarks.py work/corridor/bank/N10/r2.00   # clear one cell

Until 2026-10-23 the benchmark phases recorded only the monitor's deadlock flag,
dropping the unresolved-stall and drain-tripwire outcomes the exe reported in the
same row. Under Paul's rule of the same date those are the same outcome, so those
files cannot answer the question the paper asks of them, and dlexp_log.csv is
overwritten per seed so nothing can be recovered after the fact.

A file is stale exactly when its header lacks the drain-outcome column, so this
is driven by the file itself and not by a date: once a cell has been redone,
running it again deletes nothing. That makes it safe as the first half of a
redo job's command.
"""
import csv, glob, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
B = os.path.dirname(HERE)
NEEDS = {"benchmark.csv": ("learned_drained_clear", "pl_drained_clear"),
         "pl_benchmark.csv": ("drained_clear",),
         "hybrid_benchmark.csv": ("drained_clear",),
         "engineered_benchmark.csv": ("drained_clear",)}


def stale(path):
    base = os.path.basename(path)
    if base not in NEEDS:
        return False
    try:
        hdr = next(csv.reader(open(path, newline="")))
    except (StopIteration, OSError):
        return False
    return any(c not in hdr for c in NEEDS[base])


def main():
    if len(sys.argv) > 1:
        cells = [os.path.join(B, a) if not os.path.isabs(a) else a for a in sys.argv[1:]]
        paths = [os.path.join(c, b) for c in cells for b in NEEDS]
        act = True
    else:
        paths = glob.glob(os.path.join(B, "work", "**", "*benchmark.csv"), recursive=True)
        act = False
    n = 0
    for p in sorted(paths):
        if "_pre_twoarm" in p or not os.path.exists(p) or not stale(p):
            continue
        n += 1
        rel = os.path.relpath(p, B)
        if act:
            os.remove(p)
            print(f"[clear] removed stale {rel}")
        else:
            print(f"[clear] STALE {rel}")
    if act:
        print(f"[clear] {n} stale file(s) removed")
    else:
        print(f"[clear] {n} stale file(s); pass a cell directory to remove them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
