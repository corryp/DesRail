# Deadlock-paper full rerun on the bug-fixed exe (2026-07-05)

Orchestration + configs for the rerun. The simulator itself is **not** duplicated here —
it comes from the repo's own `DesRail/` source and `x64/Release/DesRail.exe` build.

## The single command

```
python run_all.py --jobs 14
```

(16-core VM → `--jobs 14` leaves 2 cores free. Adjust to taste.)

Run it **from inside this folder**, on the VM (the exe is a Windows binary). It is
**resumable** — kill it and re-run the same command any time; each campaign reads
its own logs in `work/` and picks up where it left off.

## What it runs (in order)

| # | campaign | what | paper |
|---|----------|------|-------|
| 1 | `corridor` | 45 cells (5 N × 9 rates), train + benchmark | §4.1 |
| 2 | `overlength` | 15 cells (5 N × 3 φ), 4-phase | §4.2 |
| 3 | `toowoomba_bsweep` | pure + hybrid, b swept, c=0.50, training-only | §4.3 |
| 4 | `toowoomba_hybrid_benchmark` | 4×3 throughput grid (PL + learned) | §4.3 |
| 5 | `overlength_ablation` | 4 stacking cells, prune ON/OFF | ablation |
| 6 | `pruning_ablation` | 4 small + 2 big corridor cells, prune ON/OFF | ablation |
| 7 | `heldout_eval` | out-of-sample residual-risk on the fixed corridor sets (native + rate-agnostic union), held-out seeds 100000+ | WP4 / residual claim |
| 8 | `coreach_verify` | coreachability check: the fixed union sets are a **no-op** on deadlock-free seeds (byte-identical `train_log`) | soundness verification |

Order matters: `toowoomba_hybrid_benchmark` consumes the constraint set learned by
`toowoomba_bsweep` (`work/toowoomba_bsweep/hybrid/b0.50_c0.50/constraints.csv`), so
the bsweep runs first; and `heldout_eval` / `coreach_verify` read the corridor
constraint sets, so they run after the corridor campaign (they are last).

The two evaluation stages write `results/heldout_eval/heldout_summary.csv` (per-cell
held-out deadlock % + CI, native vs union) and `results/coreach_verify/coreach_summary.csv`
(per-cell no-op fraction — expect 100%, confirming the fixed sets never bind on a safe
trajectory). The held-out `p(deadlock)` figure is a separate optional step needing
matplotlib: `python run_heldout_eval.py --figure` (run in a venv with matplotlib).

Run a subset with `--only` / `--skip`, e.g. `python run_all.py --jobs 14 --only corridor`.

## What changed (why the rerun)

The exe here is the **bug-fixed build** (branch `fix/prune-siding-coverage`). Root-cause
SCC classification now uses the paper's **self-containment** criterion (Def: Root-Cause
SCC) instead of the old condensed-DAG reachability test. That test could mislabel a
**derived** SCC as root-cause and emit an over-restrictive cut (a physically-free
passing-loop siding blocked by a prior constraint). Only cells that hit such a mislabel
change; cells whose SCCs were genuinely root-cause are unaffected (e.g. N5/r2.00 came out
byte-for-byte identical in validation).

## Outputs

- `results/<campaign>/…` — lean analysis CSVs (`training_log.csv`, `benchmark.csv`,
  `summary.csv`, …), small and portable. Copy these back for the paper.
- `work/<campaign>/…` — heavy per-scenario artifacts (`input/`, `output/`,
  `constraints.csv`, `warm_constraints.csv`). Stays on the VM.
- `python run_all.py --harvest-only` re-harvests `results/` from `work/` without rerunning.

## Runtime

Days, dominated by the hard cells: corridor N15/N20 at high spawn rate (deep within-seed
cascades), the Toowoomba **pure** b-sweep at b ≥ 0.50 (blows past the 25k-constraint
budget by design), and the largest overlength cells. The many small cells finish quickly.

## Contents / where the simulator comes from

This folder holds the **orchestration and configs only**: `runners/`, the top-level
`run_*.py` / `analyze_*.py` drivers, `configs/`, `configs_overlength/`, and `tmba_input/`
(the frozen Toowoomba config this campaign ran against, tracked verbatim).

The simulator is the repo's, not a copy: `run_all.py` walks up from this folder for the
directory containing `DesRail/` and exports it as `$DESRAIL_REPO`, so the runners
use `<repo>/x64/Release/DesRail.exe` and `<repo>/DesRail/generate_corridor.py`. There is
deliberately **no second copy of the C++ source here** — the earlier duplicate silently
drifted from the repo and had to be hand-resynced before the HPC build.

**Requirements:** Python 3 (standard library only) + a built `x64/Release/DesRail.exe` in
the repo root (VS2022, `Release|x64`). The appendix train-chart figure additionally needs
the Linux build, `make -C DesRail`.

**Running it elsewhere:** copy this folder plus the repo's `DesRail/` and `x64/`, or set
`$DESRAIL_REPO` to a checkout that has them — the env var overrides the walk-up.

**Note:** the exe must be built from the bug-fixed source (branch `fix/prune-siding-coverage`
or later). The campaign that produced `results/` ran against that build.
