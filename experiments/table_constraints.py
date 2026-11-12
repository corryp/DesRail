#!/usr/bin/env python3
"""Constraint counts and convergence for both stall policy arms.

The two arms exist because banking the constraints found in a stalled iteration
improved convergence, which no reviewer asked about: it is our own finding. So
this is the table where the arms have to justify themselves, and it carries
convergence beside the counts rather than the final set size alone.

Three stages have a constraint count worth reporting:

    work/corridor/<policy>/N<nn>/r<rate>/training_log.csv
    work/overlength/<policy>/N<nn>/f<frac>/training_log.csv
    results/toowoomba/<policy>/<mode>/b<b>_c0.50/training_log.csv

Convergence follows the campaign's own stopping rule, not a reading of the
final row: a cell converged if its last 100 training seeds found no deadlock,
hit the cap if it finished above 20,000 constraints, and otherwise ran out of
seeds while still learning. Those three are distinguished because they mean
different things for the paper: the third is the only one that is a result.

    python3 table_constraints.py                     # print both arms
    python3 table_constraints.py --out tables.csv    # and write the long form
"""
import os, csv, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.join(HERE, "results")
W = os.path.join(HERE, "work")
POLICIES = ["discard", "bank"]
GRID_N = [2, 5, 10, 15, 20]
GRID_R = [1.00, 1.25, 1.50, 1.75, 2.00, 2.25, 2.50, 2.75, 3.00]
OVL_F = [0.10, 0.25, 0.50]
B_VALS = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
CLEAN_WINDOW = 100
CAP = 20000


def summarise(path):
    """seeds run, final constraint count, trailing clean seeds, status."""
    try:
        rows = list(csv.DictReader(open(path, newline="")))
    except OSError:
        return None
    if not rows:
        return None
    clean = 0
    for r in reversed(rows):
        if int(r["deadlock_found"]) == 0:
            clean += 1
        else:
            break
    total = int(rows[-1]["total_constraints"])
    status = ("converged" if clean >= CLEAN_WINDOW else
              "capped" if total > CAP else "not_converged")
    return dict(seeds=len(rows), total=total, clean=clean, status=status)


def band(s):
    """One cell of the printed table: count, then what it means."""
    if s is None:
        return "-"
    mark = {"converged": "", "capped": " CAP", "not_converged": " NC"}[s["status"]]
    return f"{s['total']}{mark}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", nargs="+", default=POLICIES, choices=POLICIES)
    ap.add_argument("--out", help="write the long form here as CSV")
    a = ap.parse_args()
    long_rows = []

    def record(stage, cell, pol, s):
        if s is None:
            return
        long_rows.append([stage, cell, pol, s["seeds"], s["total"],
                          s["clean"], s["status"]])

    hdr = f"{'cell':<12}" + "".join(f"{p:>16}" for p in a.policy)
    print("=" * 90)
    print("CORRIDOR: final constraints, CAP = above 20,000, NC = still learning at the last seed")
    print(hdr)
    print("-" * 90)
    for N in GRID_N:
        for r in GRID_R:
            line = f"N{N:02d}/r{r:.2f}  "
            for pol in a.policy:
                s = summarise(f"{W}/corridor/{pol}/N{N:02d}/r{r:.2f}/training_log.csv")
                record("corridor", f"N{N:02d}/r{r:.2f}", pol, s)
                line += f"{band(s):>16}"
            print(line)

    print("\n" + "=" * 90)
    print("OVERLENGTH: final constraints")
    print(hdr)
    print("-" * 90)
    for N in GRID_N:
        for f in OVL_F:
            line = f"N{N:02d}/f{f:.2f}  "
            for pol in a.policy:
                s = summarise(f"{W}/overlength/{pol}/N{N:02d}/f{f:.2f}/training_log.csv")
                record("overlength", f"N{N:02d}/f{f:.2f}", pol, s)
                line += f"{band(s):>16}"
            print(line)

    print("\n" + "=" * 90)
    print("TOOWOOMBA b-sweep: final constraints")
    print(hdr)
    print("-" * 90)
    for mode in ["pure", "hybrid"]:
        for b in B_VALS:
            cell = f"{mode}/b{b:.2f}"
            line = f"{cell:<12}"
            for pol in a.policy:
                s = summarise(f"{R}/toowoomba/{pol}/{mode}/b{b:.2f}_c0.50/training_log.csv")
                record("toowoomba", cell, pol, s)
                line += f"{band(s):>16}"
            print(line)

    print("\n" + "=" * 90)
    print("CONVERGENCE COUNTS")
    for pol in a.policy:
        for stage in ("corridor", "overlength", "toowoomba"):
            rs = [r for r in long_rows if r[0] == stage and r[2] == pol]
            if not rs:
                continue
            tally = {}
            for r in rs:
                tally[r[6]] = tally.get(r[6], 0) + 1
            parts = " ".join(f"{k}={v}" for k, v in sorted(tally.items()))
            nc = [r[1] for r in rs if r[6] != "converged"]
            print(f"  {pol:<8} {stage:<11} {len(rs):>3} cells: {parts}"
                  + (f"   [{', '.join(nc)}]" if nc else ""))

    if a.out:
        with open(a.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["stage", "cell", "policy", "seeds", "final_constraints",
                        "trailing_clean_seeds", "status"])
            w.writerows(long_rows)
        print(f"\nwrote {len(long_rows)} rows to {a.out}")


if __name__ == "__main__":
    main()
