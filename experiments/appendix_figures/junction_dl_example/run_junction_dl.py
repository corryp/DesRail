#!/usr/bin/env python3
"""
Appendix E (fig:tmba_anim / fig:tmba_scc) regeneration: the constraint-induced
3-train deadlock at the Toowoomba junction.

Scenario (matches the paper): lambda_B=1.75, lambda_C=0.75, PL constraints ON,
seed 1, deadlock_avoidance_exp. Iteration 0 -> a 2-train deadlock -> dl_0
(2-tuple). Iteration 1 (dl_0 active) -> a 3-train circular wait {T18,T16,T22}
-> dl_1 (3-tuple). We run 2 iterations with START_DEBUG=1 so iteration 1 dumps
its conflict graph (.dot) and animates (anim_script.json).

Uses the bundle's current exe (full-backer fix) and the Toowoomba input as-is
(train_white + RGB templates -> colour-coded trains/markers).

  python run_junction_dl.py            # run + verify SCC/tuples, save dot+anim
"""
import csv
import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, CAMPAIGN_DIR)
sys.path.insert(0, os.path.join(CAMPAIGN_DIR, "runners"))
from run_overlength_exp import set_csv_option, run_exe  # noqa: E402
from _run_toowoomba_helpers import set_toowoomba_rates  # noqa: E402

TMBA_INPUT = os.path.join(CAMPAIGN_DIR, "DesRail", "input")
B_RATE = 1.75
C_RATE = 0.75
SEED = 1
WARMUP = 24
HORIZON = 168 + WARMUP
DEBUG_ITER = 1            # animate + DOT on iteration 1 (the 3-train deadlock)

WORKDIR = os.path.join(SCRIPT_DIR, "work")
OUT_DIR = os.path.join(SCRIPT_DIR, "out")


def raw_rows(path):
    with open(path, newline="") as fh:
        return [r for r in csv.DictReader(fh)
                if (r.get("iteration") or "").strip().isdigit()]


def setup():
    input_dir = os.path.join(WORKDIR, "input")
    output_dir = os.path.join(WORKDIR, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    for fn in os.listdir(TMBA_INPUT):
        s = os.path.join(TMBA_INPUT, fn)
        if os.path.isfile(s) and fn.endswith(".csv"):
            shutil.copy2(s, os.path.join(input_dir, fn))

    # Pure learning under PL: no manual signal constraints, no hangover nogoods.
    with open(os.path.join(input_dir, "signal_constraints.csv"), "w", newline="") as f:
        f.write("str_id,segment / occ_limit,head,target,comment\n")
    nogood = os.path.join(input_dir, "nogood_constr.csv")
    if os.path.exists(nogood):
        os.remove(nogood)

    set_toowoomba_rates(os.path.join(input_dir, "open_loop_spawners.csv"),
                        B_RATE, C_RATE)

    runctrl = os.path.join(input_dir, "runctrl.csv")
    set_csv_option(runctrl, "sim_len", HORIZON)
    set_csv_option(runctrl, "warmup", WARMUP)
    set_csv_option(runctrl, "seed", SEED)
    set_csv_option(runctrl, "pl_constraints", 1)     # PL active (per paper)
    set_csv_option(runctrl, "screen_output", 0)
    set_csv_option(runctrl, "animate", 1)

    set_csv_option(os.path.join(input_dir, "main_option.csv"),
                   "enter experiment type", "deadlock_avoidance_exp")

    dlexp = os.path.join(input_dir, "dlexp_options.csv")
    set_csv_option(dlexp, "MAX_ITERATIONS", DEBUG_ITER + 1)
    set_csv_option(dlexp, "START_DEBUG", DEBUG_ITER)
    set_csv_option(dlexp, "CONSTRAINT_EVAL", "flat")
    set_csv_option(dlexp, "WARM_START_FILE", "")
    set_csv_option(dlexp, "LAST_CONSTRAINT", "")
    return input_dir, output_dir


def main():
    input_dir, output_dir = setup()
    anim_dir = os.path.join(WORKDIR, "..", "Animation")
    os.makedirs(anim_dir, exist_ok=True)
    print(f"JUNCTION: b={B_RATE}, c={C_RATE}, seed={SEED}, PL=1, "
          f"MAX_ITER={DEBUG_ITER+1}, START_DEBUG={DEBUG_ITER}")
    run_exe(WORKDIR)

    os.makedirs(OUT_DIR, exist_ok=True)
    log = os.path.join(output_dir, "dlexp_log.csv")
    for r in raw_rows(log):
        print(f"  iter {r['iteration']}: dl={r['deadlock_found']} "
              f"t={r['deadlock_time']} +{r['new_constraints']}c "
              f"total={r['total_constraints']} details={r.get('constraint_details')} "
              f"pruning={r.get('pruning_details')}")

    dot = os.path.join(output_dir, f"conflict_graph_{DEBUG_ITER}.dot")
    if os.path.exists(dot):
        shutil.copy2(dot, os.path.join(OUT_DIR, f"conflict_graph_{DEBUG_ITER}.dot"))
        print(f"  -> out/conflict_graph_{DEBUG_ITER}.dot")
    anim = os.path.normpath(os.path.join(anim_dir, "anim_script.json"))
    if os.path.exists(anim):
        shutil.copy2(anim, os.path.join(OUT_DIR, f"anim_iter{DEBUG_ITER}.json"))
        print(f"  -> out/anim_iter{DEBUG_ITER}.json")

    gen = os.path.join(output_dir, "generated_constraints.csv")
    if os.path.exists(gen):
        shutil.copy2(gen, os.path.join(OUT_DIR, "generated_constraints.csv"))
        print("  -> out/generated_constraints.csv")


if __name__ == "__main__":
    main()
