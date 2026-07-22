# DesRail — Deadlock-Avoidance Companion (`deadlock-paper` branch)

This branch is the reproducibility companion to:

> *Learned Deadlock Avoidance for Rail Network Simulation via Iterative No-Good Cut Generation*. Paul Corry (in preparation).

It contains the DesRail simulator **with the deadlock-avoidance learner**, the experiment runners, and the raw results behind the paper's three campaigns (corridor, overlength, Toowoomba junction), the pruning ablation, and the out-of-sample evaluation stages.

The results here come from the 2026-07 rerun on the corrected build. Two changes since the previous drop: root-cause SCC classification now uses the paper's self-containment criterion (the old condensed-DAG reachability test could mislabel a derived SCC as root-cause and emit an over-restrictive cut), and the simulator gained a drain / `start_warmdown` horizon so a deadlock forming just before the end of the run is still detected. Every campaign was rerun end to end on that build.

> For the core simulator and the first paper's data, see the [`main`](https://github.com/corryp/DesRail/tree/main) branch. This branch extends that code with the deadlock work and ships the deadlock-paper experiments.

DesRail is developed under the **DESLEARN** research programme (machine learning + discrete-event simulation in railway operations research).

---

## The deadlock-avoidance method, in brief

Trains acquire signal-to-signal sections through a `SignalsManager`. When a run deadlocks, the simulator analyses the conflict (Tarjan SCC over a section-blocking graph), derives a **no-good cut** — an occupancy constraint on a group of signal arcs — and adds it to the constraint set. Re-running with the augmented set and iterating until a clean horizon converges on a constraint set that provably prevents the observed deadlocks. See `ARCHITECTURE.md` and `DesRail/deadlock_avoidance.*` for detail.

## Building the simulator

### Reference build — Visual Studio 2022 (Windows)

1. Open `DESLEARN.sln` in Visual Studio 2022 (v143 toolset, C++20).
2. Select `Release | x64`.
3. Build (Ctrl+Shift+B). The executable is produced at **`x64/Release/DesRail.exe`** (repo root).

### Portable build — make (Linux, macOS, MinGW)

```bash
cd DesRail
make            # produces ./desrail
```

Requires a C++20 compiler with coroutine support (GCC ≥ 11, Clang ≥ 14, MSVC ≥ v143).

## Running the experiments

> **Executable-location assumption.** There is exactly one copy of the simulator
> in this repository, at `DesRail/`. The experiment runners find it by walking up
> from their own directory for the first one containing `DesRail/makefile`, then
> expect the binary at `<that dir>/x64/Release/DesRail.exe`. The repository root
> satisfies this out of the box, so after the reference Visual Studio build above
> the runners need no extra configuration.
>
> To point the runners at a binary elsewhere, set the `DESRAIL_REPO` environment
> variable to a directory containing `DesRail/` and `x64/Release/DesRail.exe`.
> The runners are Windows-oriented: they invoke `DesRail.exe`, so reproducing the
> campaigns expects the Windows build. (The appendix train-chart figure is the one
> exception -- it runs the Linux binary from `make`.)

The full sequence (corridor, overlength, Toowoomba, pruning ablation, then the
two out-of-sample evaluation stages):

```bash
cd experiments
python run_all.py --jobs 12              # all campaigns in sequence
python run_all.py --only corridor        # a single campaign
python run_all.py --setup-only           # create dirs + configs only
```

Individual campaign runners live in `experiments/runners/`:

| Campaign | Runner |
|---|---|
| N-loop corridor | `runners/run_corridor.py` |
| Overlength trains | `runners/run_overlength.py` |
| Toowoomba junction (b-sweep + hybrid) | `runners/run_toowoomba_bsweep.py`, `runners/run_toowoomba_hybrid_benchmark.py` |
| Pruning ablation | `runners/run_pruning_ablation.py`, `runners/run_overlength_ablation.py` |
| Held-out residual risk | `run_heldout_eval.py`, `run_heldout_drained.py` |
| Coreachability (no-op) check | `coreach_verify.py`, `run_coreach_drained.py` |

The last two are evaluation rather than training. `run_heldout_eval.py` measures
out-of-sample residual deadlock risk for a learned set on seeds it never saw;
`coreach_verify.py` checks the soundness claim directly, that a learned set is a
byte-identical no-op on runs that were already deadlock-free. The `*_drained`
variants re-run both under the drain / `start_warmdown` horizon, which closes a
blind spot where a deadlock forming just before the horizon went undetected and
made a doomed seed look safe. `analyze_drained.py` and `digest_all.py` summarise
them; `COREACH_R200_DIAGNOSIS.md` and `gate_a_results.txt` record that analysis.

`run_experiments.py` is the canonical 45-scenario corridor driver and also
provides the shared helpers the `runners/` scripts import. The overlength
campaign's canonical driver is `runners/run_overlength.py` (the top-level
`run_overlength_exp.py` is retained because the ablation and appendix-figure
scripts import helpers from it; do not use it as the overlength campaign driver).

Analysis:

```bash
python analyze_results.py        # corridor / general
python analyze_overlength.py     # overlength
```

## Results

Raw results are under `experiments/results/`:

| Folder | Campaign |
|---|---|
| `corridor/` | N-loop corridor deadlock-avoidance (paper §4.2–4.4) |
| `overlength/` | Overlength-train campaign |
| `toowoomba_bsweep/` | Toowoomba junction rate sweep (pure + hybrid), training logs and learned constraint sets |
| `toowoomba_hybrid_benchmark/` | Hybrid (engineered + learned) benchmark |
| `pruning_ablation/`, `overlength_ablation/` | Constraint-set pruning ablation |
| `heldout_eval/`, `heldout_drained/` | Out-of-sample residual-risk evaluation |
| `coreach_verify/`, `coreach_drained/` | Coreachability no-op check |

Each cell carries both its `training_log.csv` and the `constraints.csv` it converged
on. The four `toowoomba_bsweep/pure/b*` sets at λ_B ≥ 0.50 are large (9–12 MB) by
design: those cells deliberately run past the 20,000-constraint budget without
converging, which is the result being reported.

`experiments/appendix_figures/` holds the setups behind the paper's appendix figures:

- `junction_train_charts/` — the Toowoomba before/after string-line diagram.
- `monotone_convergence/` — within-seed monotone convergence for the overlength
  N=10, φ=0.25 cell. Its per-seed traces correspond to `results/overlength/N10/f0.25`
  seed for seed (2000 / 2000 / 687 new constraints, 4,687 total); that cell is
  unaffected by the root-cause classification fix, so the trajectory is unchanged.
- `overlength_dl_example/` — the conflict graph and animation snapshot behind the
  paper's overlength deadlock example: the N=10, φ=0.25 overlength scenario, seed 1.
  Two figures come from it, the initial 2-train physical deadlock with no pre-loaded
  constraints, and the constraint-induced deadlock after 30 training iterations where
  pruning reduces a 4-train SCC to its 3-train core.
- `junction_dl_example/` — the same for the Toowoomba junction example: λ_B = 1.75,
  λ_C = 0.75, passing-loop constraints active, seed 1, second training iteration with
  `dl_0` active, where three trains form a circular wait at the junction.

## Documentation

- `ARCHITECTURE.md` — full architectural walkthrough.
- `docs/csv_schemas.md` — CSV input file reference.

## Citation

```bibtex
@misc{corryDeadlock2026,
  title  = {Learned Deadlock Avoidance for Rail Network Simulation via Iterative No-Good Cut Generation},
  author = {Corry, Paul},
  year   = {2026},
  note   = {In preparation}
}
```

## License and attribution

See [LICENSE](LICENSE) and [ATTRIBUTION](ATTRIBUTION). The Queensland rail
network data carries its own attribution terms documented there.

## Contact

Paul Corry — Queensland University of Technology — `p.corry@qut.edu.au`
