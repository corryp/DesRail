# DesRail — Deadlock-Avoidance Companion (`deadlock-paper` branch)

This branch is the reproducibility companion to:

> *Learned Deadlock Avoidance for Rail Network Simulation via Iterative No-Good Cut Generation*. Paul Corry (in preparation).

It contains the DesRail simulator **with the deadlock-avoidance learner**, the experiment runners, and the raw results behind the paper's three campaigns (corridor, overlength, Toowoomba junction) plus the pruning ablation.

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

> **Executable-location assumption.** The experiment runners locate the
> simulator by walking up from `experiments/runners/` for the first directory
> that contains **both** a `DesRail/` folder and an `x64/` folder, and then
> expect the binary at `<that dir>/x64/Release/DesRail.exe`. After the
> reference Visual Studio build above, the repository root satisfies this
> (it has `DesRail/` and the build emits `x64/Release/DesRail.exe`), so the
> runners find the exe with no extra steps.
>
> To point the runners at a binary elsewhere, set the `DESRAIL_REPO`
> environment variable to a directory that contains `DesRail/` and
> `x64/Release/DesRail.exe`. The runners are Windows-oriented: they invoke
> `DesRail.exe`, so reproducing the campaigns expects the Windows build.

The full sequence (corridor, overlength, Toowoomba, pruning ablation):

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
| `toowoomba/` | Toowoomba junction b-sweep (pure + hybrid) |
| `toowoomba_hybrid_benchmark/` | Hybrid (engineered + learned) benchmark |
| `pruning_ablation/` | Constraint-set pruning ablation |

`experiments/appendix_figures/` holds the deadlock-example and convergence
setups used for the paper's appendix figures (animation + SCC graphs).

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
