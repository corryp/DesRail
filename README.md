# DesRail: deadlock-avoidance companion (`deadlock-paper` branch)

This branch is the code companion to:

> Paul Corry, Robert Burdett and Erhan Kozan. *Learning No-Good Cuts from Simulation for Deadlock Avoidance in Rail Networks*. Under review at Transportation Research Part C (submitted as *Learned Deadlock Avoidance for Rail Network Simulation via Iterative No-Good Cut Generation*).

It contains the DesRail simulator with the deadlock-avoidance learner, and the experiment runners, cluster scripts and analysis scripts behind every experiment in the paper. The tag `trc-revision-2026-11` marks the state that accompanies the revised manuscript.

**The results are not in this repository.** The simulation outputs, learned constraint sets and held-out evaluations, with READMEs mapping each table and figure to its files, are in a separate data deposit: [doi:10.5281/zenodo.24150753](https://doi.org/10.5281/zenodo.24150753). Earlier states of this branch carried results from superseded runs; those are in the branch history only.

> For the core simulator and the DesRail paper's data, see the [`main`](https://github.com/corryp/DesRail/tree/main) branch. DesRail is developed under the DESLEARN research programme (machine learning and discrete-event simulation in railway operations research).

---

## The method, in brief

Trains acquire signal-to-signal sections through a `SignalsManager`. When a run deadlocks, the simulator builds a conflict graph over the trains involved, finds its strongly connected components with Tarjan's algorithm, reduces a root-cause component to a minimal self-contained core, and generates a no-good cut: a constraint that denies any grant leading to the configuration the core occupied. The run is repeated with the augmented set, and training continues over fresh random seeds until a stopping threshold of consecutive clean replications is reached. See `ARCHITECTURE.md` and `DesRail/deadlock_avoidance.*`.

## Which code produced the results

`DesRail/` is the engine at DESLEARN commit `d8775c7`. Every run in the data deposit was produced by one binary built from this source (md5 `f386c9aa035c52afd8e958f638850093`, recorded per run in the deposit's manifests). `experiments/` holds the runners and scripts as the campaign ran them, including those for stages added after the main run.

## Building the simulator

Portable build (Linux, macOS, MinGW), which is how the campaign was built on the cluster (GCC 13.3, C++20):

```bash
cd DesRail
make            # produces ./desrail
```

Reference Windows build: open `DESLEARN.sln` in Visual Studio 2022 (v143 toolset, C++20), select `Release | x64` and build; the executable is `x64/Release/DesRail.exe`.

A C++20 compiler with coroutine support is required (GCC 11 or later, Clang 14 or later, MSVC v143).

## Running the experiments

The runners find the repository by walking up from their own directory to the first folder containing `DesRail/makefile`. They run `x64/Release/DesRail.exe` under it by default; set `DESRAIL_EXE` to the path of the Linux binary (`DesRail/desrail`) to use the portable build. Every run uses the drained protocol (horizon 1000 h, spawning stopped at 192 h, statistics window 24 to 192 h), which the runners apply themselves; a config read on its own shows the undrained horizon. The stall policy, `bank` or `discard`, is read from `STALL_POLICY` and appears in every output path; the paper reports `discard`.

On a PBS cluster, as the paper's campaign was run:

```bash
cd experiments
python pbs/run_harness.py --dry-run      # show which cells would be submitted
python pbs/run_harness.py                # submit every cell whose dependencies are met
python pbs/run_harness.py --status
```

Each cell is one single-threaded job (`pbs/cell.pbs`, which carries the QUT project directive `-P`; change it for another site). Later stages are submitted by the `pbs/submit_*.sh` scripts, each of which documents its own usage: held-out benchmarks, full-union evaluations, per-route waits, per-train delays, the 6N engineered re-benchmark, the pure held-out benchmark, the stopping-threshold sweep and the Figure 4 scan.

On a single machine, `python run_all.py --jobs <n>` runs the training and benchmark campaigns and the ablations in sequence; the held-out evaluation, the coreachability check and the later stages are run with the scripts in the table below. `README_RERUN.md` describes the 2026-07 rerun this layout comes from.

| Experiment | Runner |
|---|---|
| Corridor training and benchmark | `runners/run_corridor.py` |
| Overlength trains | `runners/run_overlength.py` |
| Toowoomba junction rate sweep and benchmark | `runners/run_toowoomba_bsweep.py`, `runners/run_toowoomba_hybrid_benchmark.py` |
| Pruning ablations | `runners/run_pruning_ablation.py`, `runners/run_overlength_ablation.py` |
| Held-out residual risk | `run_heldout_drained.py` |
| Coreachability no-op check | `run_coreach_drained.py` |
| Stopping-threshold sweep | `runners/run_r_sweep.py` |
| Per-train delays and occupancy | `extract_train_delays.py`, `collect_train_delays.py`, `figures/` |

The corridor and overlength networks are generated by `DesRail/generate_corridor.py`; the generated configurations the campaign used are in `configs/` and `configs_overlength/`, and the engineered spawn-hold set by `DesRail/generate_overlength_constraints.py --no-lock-through`. The Toowoomba network is not parametric and is in `tmba_input/`. The tables were read from the results by `table_constraints.py`, `table_residual.py`, `table_delay.py` and `table_overlength.py`.

`experiments/appendix_figures/` holds the setups behind the appendix figures: `junction_train_charts/` (the Toowoomba before-and-after train chart), `overlength_dl_example/` (the overlength deadlock example, N = 10, overlength fraction 0.25, seed 1) and `junction_dl_example/` (the Toowoomba junction example).

## Documentation

- `ARCHITECTURE.md`: architectural walkthrough.
- `docs/csv_schemas.md`: CSV input file reference.

## Citation

```bibtex
@misc{corryDeadlockData2026,
  title        = {{DesRail}: Data and Results for ``Learning No-Good Cuts from Simulation for Deadlock Avoidance in Rail Networks''},
  author       = {Corry, Paul and Burdett, Robert and Kozan, Erhan},
  year         = {2026},
  howpublished = {Zenodo},
  doi          = {10.5281/zenodo.24150753}
}
```

## License and attribution

See [LICENSE](LICENSE) and [ATTRIBUTION](ATTRIBUTION). The Queensland rail network data carries its own attribution terms, documented there.

## Contact

Paul Corry, Queensland University of Technology, `p.corry@qut.edu.au`
