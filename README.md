# DesRail

A high-performance discrete-event simulator for rail traffic operations, with a Pyglet-based animation viewer for playback.

DesRail is developed under the **DESLEARN** research programme, which investigates the integration of machine learning with discrete-event simulation in railway operations research. This repository is the public companion to:

- *Generalised, flexible and scalable high-performance discrete-event-simulation for rail network operations*. Corry, Burdett, Dendle, Kozan ([SSRN preprint](https://ssrn.com/abstract=6424218)).
- *Learned Deadlock Avoidance for Rail Network Simulation via Iterative No-Good Cut Generation*. Corry (in preparation).

> **Deadlock-avoidance work lives on a separate branch.** The iterative no-good-cut deadlock learner, its experiment runners, and the deadlock-paper results are on the [`deadlock-paper`](https://github.com/corryp/DesRail/tree/deadlock-paper) branch. This `main` branch holds the core simulator and the data behind the first paper.

---

## What you can do with this code

- Build a rail network from CSV inputs (track segments, signals, terminals, passing loops, train templates).
- Run discrete-event simulations of train movements with realistic acceleration, signal locking, and section-based access control.
- Spawn open-loop train traffic at configurable rates and routes.
- Play back any simulation as a 2D animation with pan/zoom and configurable rendering.

The simulator is built on C++20 coroutines for processes (trains, spawners, monitors), giving compact, readable process definitions while staying performant.

## Architecture

```
┌─────────────────────┐    anim_script.json    ┌──────────────────────┐
│  C++ Simulator      │  ────────────────────► │  Python Visualizer   │
│  DesRail/           │                        │  Animation/          │
│  - DES engine       │      CSV inputs        │  - DesViz framework  │
│  - Rail network     │  ◄──────────           │  - Pan/zoom playback │
└─────────────────────┘                        └──────────────────────┘
```

The C++ simulator reads CSV configuration, runs the simulation, and emits an animation script. The Python viewer is optional and consumes that script for after-the-fact playback (it is *not* a real-time renderer).

## Building

### Reference build — Visual Studio 2022 (Windows)

1. Open `DESLEARN.sln` in Visual Studio 2022 (v143 toolset, C++20).
2. Select `Release | x64`.
3. Build (Ctrl+Shift+B). The executable is `x64/Release/DesRail.exe`.

### Portable build — make (Linux, macOS, MinGW)

```bash
cd DesRail
make            # produces ./desrail
```

Requires a recent compiler with C++20 coroutine support (GCC ≥ 11, Clang ≥ 14, MSVC ≥ v143).

### Python visualizer

```bash
cd Animation
python -m venv env
source env/bin/activate            # Windows: env\Scripts\activate
pip install pyglet
```

## Running

### Configuration inputs

The simulator reads its scenario from an `input/` directory of CSV files, and writes
to an `output/` directory which must exist before a run. Both are included here.

`DesRail/input/` holds a Toowoomba regional network scenario **so that the simulator is
runnable as soon as it is built**. It is an example, not an experiment: it does not
correspond to any result reported in the papers, and it should not be used to reproduce
one. The data behind the published results is deposited separately, as described under
Data and results below.

CSVs can also be dumped from the workbook `DesRail/input/A_CSVdump*.xlsm`, which is the
source of truth for a scenario built that way: open the workbook and run its export macro
to (re)generate them.

### A single simulation

The simulator expects to run from a directory containing an `input/` subfolder
of CSV configs and an `output/` subfolder for results:

```bash
cd DesRail                  # contains input/ and output/
./desrail                   # or x64\Release\DesRail.exe on Windows
```

### Playing back the animation

```bash
cd Animation
python DesRailAnim.py
# or with custom paths:
python DesRailAnim.py --paths /path/to/Paths.csv \
                      --config /path/to/anim_config.json \
                      --script /path/to/anim_script.json
```

## Data and results

**The data are deposited separately from this repository**, which holds the simulator only:
https://doi.org/10.5281/zenodo.23308676
The deposit carries the configured scenarios, the runs behind every table and figure, the
raw transit-feed capture the urban case study descends from, and the scripts that turn one
into the other. It is cited in the paper by its own identifier.

The data from the first version of the simulator paper, computed on an earlier network
extraction that has since been corrected, are preserved on the
[`desrail-paper-v1-stale`](https://github.com/corryp/DesRail/tree/desrail-paper-v1-stale)
branch. They are kept for the record and should not be used: the extraction gave incorrect
segment lengths, and every result that depended on them has been re-run.

## Repository layout

```
DesRail (main branch)
├── DesRail/              C++ simulator (entry: main.cpp; build: DESLEARN.sln or makefile)
│   ├── desviz_src/       Animation-script writer (used by the simulator)
│   ├── input/            Example scenario, so the simulator runs out of the box
│   └── output/           Where a run writes its animation script and logs
├── Animation/            Python playback viewer (DesViz framework)
└── DESLEARN.sln          Visual Studio solution
```

## Citation

```bibtex
@misc{corryDesRail2025,
  title  = {Generalised, Flexible and Scalable High-Performance Discrete-Event-Simulation for Rail Network Operations},
  author = {Corry, Paul and Burdett, Robert L. and Dendle, Nicholas and Kozan, Erhan},
  year   = {2026},
  doi    = {10.2139/ssrn.6424218},
  url    = {https://ssrn.com/abstract=6424218}
}
```

## License and attribution

See [LICENSE](LICENSE) and [ATTRIBUTION](ATTRIBUTION). The Queensland rail
network data carries its own attribution terms documented there.

## Contact

Paul Corry — Queensland University of Technology — `p.corry@qut.edu.au`
