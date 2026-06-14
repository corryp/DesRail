# DesRail Architecture

This document is a tour of the codebase intended for someone reading it for the first time. It covers what each component does, how the pieces fit together, and the design choices that shaped the implementation. For build instructions and a quick-start, see [README.md](README.md). For input file formats, see [docs/csv_schemas.md](docs/csv_schemas.md).

## High-level shape

```
                 ┌──────────────────────────────────────────────────────┐
                 │  C++ Simulator (DesRail/)                            │
                 │                                                      │
   CSV inputs ──►│   ┌──────────────┐    ┌──────────────────────────┐   │
                 │   │ DES engine   │◄──►│  Rail-domain model       │   │
                 │   │ simulator.h  │    │  desrail.h, .cpp         │   │
                 │   └──────────────┘    └──────────────────────────┘   │
                 │          ▲                       ▲                   │
                 │          │                       │                   │
                 │   ┌──────┴───────────────────────┴────────────┐      │
                 │   │  Orchestration: rail_sim_tools.h, .cpp    │      │
                 │   │  Deadlock learner: deadlock_avoidance.*   │      │
                 │   └───────────────────────────────────────────┘      │
                 │                       │                              │
                 │                       ▼                              │
                 │                  output/* + anim_script.json         │
                 └──────────────────────────────────────────────────────┘
                                         │
                                         ▼
                 ┌──────────────────────────────────────────────────────┐
                 │  Python Visualizer (Animation/)                      │
                 │  Pyglet-based playback of anim_script.json           │
                 └──────────────────────────────────────────────────────┘
```

The simulator is the core. The Python side is a playback animator that reads a JSON event stream — there is no live coupling, which means simulations run as fast as the CPU allows and the animation can be watched, scrubbed, or skipped without affecting the simulation.

## C++ Simulator

### DES Engine — `simulator.h` / `simulator.cpp`

A small, general-purpose discrete-event simulation engine built on C++20 coroutines. Three core types:

- **`Sim`** owns the global event queue (`priority_queue<SimQueuedEvent*>` ordered by time, then priority), the list of pending conditional events, and the current simulation time. Its `run()` repeatedly calls `step()`, which pops the next event, advances time, and resumes the associated coroutine until the queue is empty or `max_time` is reached.

- **`SimObject`** is the base class for any process. Subclasses override `run()` (which returns a `SimCoroutine`) and use the awaitables `delay(t)`, `sleep()`, and `wait_until(predicate)` to suspend themselves. The compiler stores the coroutine frame; locals across `co_await` are preserved automatically.

- **`SimCoroutine`** is the return type of `run()`. It wraps a `std::coroutine_handle<SimPromise>`. The `SimPromise` uses `suspend_never final_suspend()` so coroutine frames remain alive after their `run()` returns — `Sim::reset()` clears them in bulk between iterations of an experiment.

The engine knows nothing about trains, tracks, or rails. The same machinery could drive an arbitrary discrete-event system.

### Rail-domain model — `desrail.h` / `desrail.cpp`

All rail-specific types live here. Every type has an integer ID, populated from CSV input, that survives across simulation rebuilds — this matters for the deadlock learner, which serializes constraints by ID.

- **`RailNetwork`** owns `nodes`, `segments`, `sections`, `terminals`, `arcs`, and the `SignalsManager`. Built once from CSV in `initialise()` / `process_network()`. `reset(Sim&)` is called between experiment iterations to recreate run-time state without re-reading files.

- **`TrackNode`** — junction point with in/out arc lists.

- **`TrackSegment`** — physical track between two nodes. State machine: `FREE` → `LOCKED` → `LOADED`. Owns two `DirectedSegment` arcs (forward and reverse), one of which may be disabled.

- **`DirectedSegment`** — an oriented arc (segment + head node). Carries the signal flag, margin, terminal, speed limit, priority, current owner, and the list of `SignalConstraint`s registered on it. Most of the simulation acts at the arc level rather than the segment level.

- **`TrackSection`** — an ordered sequence of arcs from one signal to the next (a signal-to-signal block). `is_available()` and per-terminal `reachable[]` flags govern whether a train can request access.

- **`Train`** is itself a `SimObject` and therefore a coroutine. Its `run()` traces a state machine: request next section → compute speed profile → traverse arcs → dwell at terminals. It spawns helper coroutines (`TrainUpdateTrigger` for motion events, `DelayedTrackRelease` for the trailing-edge of segment release) and awaits them.

- **`SignalsManager`** is the gatekeeper for section access. `request_access()` calls `grant_access()`, which checks `is_available()`, the `reachable[]` flags, and every registered `SignalConstraint::is_violated()`. If denied, the request goes onto an internal queue; when segments free up, the manager wakes and re-evaluates.

- **`SignalConstraint`** is an occupancy constraint defined on a set of arcs (or, for generated constraints, segments). `is_violated()` counts distinct trains occupying or requesting the arcs (subject to optional target matching) and compares against an occupancy limit. Two evaluation paths exist: `is_violated()` (arc-level, manual constraints from `signal_constraints.csv`) and `is_violated_seg()` (segment-level, used by generated constraints).

### Orchestration — `rail_sim_tools.h` / `rail_sim_tools.cpp`

- **`RailSimMaster`** is the top-level driver. Its constructor reads all CSV configs, builds the `RailNetwork`, and prepares the simulation. `run()` invokes the chosen experiment mode and returns a summary log.

- **`OpenLoopTrainSpawner`** is a coroutine that generates trains at intervals sampled from a configured distribution (`uniform`, `normal`, `exponential`). Each spawner has a route — a sequence of `(terminal, dwell_time)` tasks — that becomes the spawned train's `Job`.

- **`PassingLoopGroup`** is a configuration helper that, given the four arcs of a passing-loop pattern (mainline-0, mainline-1, siding-0, siding-1, plus approach signals), generates the standard set of signal constraints that prevent unsafe passing-loop crossings.

- **`DistributionSampler`** wraps the chosen `<random>` distribution behind a uniform `sample()` interface.

### Animation output — `dr_anim.h` / `dr_anim.cpp` and `desviz_src/`

When animation is enabled, the simulator emits an `anim_script.json` describing every visible event: object creation, path-following motion, color changes, custom commands. The format is documented in `Animation/CLAUDE.md` (until that's split into an Animation user guide).

## Deadlock avoidance subsystem — `deadlock_avoidance.h` / `deadlock_avoidance.cpp`

This is the algorithmic core of the second paper. Given a network where deadlocks occur, it iteratively learns signal constraints that prevent them.

### The iterative loop

```
loop:
   run a simulation with the current constraint set
   if it completes deadlock-free:
      if 100 consecutive seeds have been clean: converged → exit
      else: increment seed, repeat
   else:
      build a conflict graph from the deadlocked state
      find non-singleton SCCs (Tarjan's algorithm)
      generate one no-good constraint per root-cause SCC
      add to the constraint set, restart with the same seed
```

### Components

- **`DeadlockMonitor`** is a `SimObject` coroutine that runs alongside the simulation. Periodically (`deadlock_check_interval`) it inspects all blocked train-section requests and looks for any whose age exceeds `deadlock_timeout`. Such requests are candidates for being part of a deadlock cycle. The monitor runs off the hot path, so it adds no per-request overhead.

- **`ConflictGraph`** is built from the network state at deadlock time. One node per blocked train, plus an "omega" sink representing trains that are still progressing. Edges encode "X is blocked by Y" with a label indicating whether the block is physical (`S:`) or constraint-induced (`C:`). The graph is the input to the SCC analysis.

- **Tarjan's SCC** (`tarjan_scc()`) finds strongly connected components in `O(V+E)`. Non-singleton SCCs that cannot reach omega are deadlock cycles.

- **Root-cause filtering** condenses the SCC DAG and keeps only SCCs that are sinks (no path to a smaller deadlock SCC). This avoids generating constraints for derivative deadlocks that will be resolved when the root cause is fixed.

- **Redundant edge pruning** trims constraint-induced edges from each SCC where doing so doesn't change the SCC's structure. This prevents constraint-set inflation across iterations.

- **`GeneratedConstraint`** is the serialisable form: a set of `SegTargetSpec`s (segment ID, terminal name, front-signal segment/node IDs, occupancy limit) that survives a network rebuild. `apply_generated_constraints()` reads them back into live `SignalConstraint` objects on the rebuilt network.

### Design choices worth knowing

- **Constraints are by ID, not pointer.** A constraint generated in iteration N can be applied to the network rebuilt in iteration N+1 — the IDs resolve to the new objects.
- **Same random seed every iteration**, so the simulation is deterministic up to the point where new constraints change behaviour. This makes deadlock cycles reproducible while training.
- **Generated constraints use segment-level qualification** (`is_violated_seg()`): the tuple is `(segment, front_signal, target)` rather than the arc-level `(segment, head_node, front_signal, target)` of manual constraints. The assumption — verified empirically on all corridor results — is that trains with a given target only occupy each segment in one direction.
- **Merge-and-lower**: when a learned constraint has the same geometry as a pre-cooked engineered one, the engineered one's `min_length` threshold is lowered to cover the shorter trains too, rather than adding a duplicate.

## Python visualization — `Animation/`

Pyglet-based playback. Not real-time; consumes a pre-recorded `anim_script.json`.

### Layered design

- **`desviz/DesViz.py`** is the domain-agnostic core. It knows nothing about trains.
  - **`DesVizMaster`** extends `pyglet.window.Window`. Owns the rendering batch, the simulation clock, and up to ten subwindow render layers.
  - **`SubWindow`** is a coordinate-transformed viewport. `anim_xy(x_ext, y_ext)` maps external (UTM, model) coordinates to window pixels via a configurable scale and reference offset.
  - **`DesVizObject`** is a sprite wrapper with movement logic — `place`, `move` (linear interpolation), `move_on_path` (with optional acceleration), `place_on_path`. Supports leader/follower chains for consist cars.
  - **`DesVizPath`** is a sequence of `PathSegment`s built from waypoints. `calc_xy(rel_pos)` maps `[0,1]` to `(x, y, segment)`.
  - **`CtrlWindow`** is the simulation-clock and speed-control overlay (render layer 9, drawn over everything else).

- **`DesRailAnim.py`** is the rail-specific entry point. It loads `Paths.csv`, draws the track lines, registers the `update_arc_state` custom command for section colour changes, and creates the lock-line rectangles that visualise locked sections.

The clean separation means `desviz/` could in principle be lifted into a standalone package and used to animate any system whose state can be described as object-on-path movements over time.

### Coordinate system

In `draw_paths` mode, external coordinates are typically UTM easting/northing (large values like 501000, 6962000). `SubWindow.anim_xy()` applies a scale factor and reference offset to map them to window pixels. The window's bounding box is auto-computed from `Paths.csv` at startup.

In `pixel_units` mode, external coordinates are treated as already-pixel and an identity transform is applied. Used when laying objects over a background image.

### Animation script format

JSON with three top-level keys: `version`, `metadata`, and `commands`. Each command has `data: {time, command, args, kwargs}`. The reader processes commands in order each frame: every command with `time <= sim_clock` is executed.

Commands are dispatched via the `@tag_command` decorator system in `desviz/logging_core.py`. Each method is registered under one or more aliases (e.g. `add_object` / `add` / `add_at_point`). User code can register custom commands via `subwindow.custom_commands[alias] = handler` — this is how `DesRailAnim.py` adds the rail-specific `update_arc_state` command without touching the DesViz core.

## Conventions and invariants

### Floating-point comparisons

Never use raw `>`, `<`, `==`, `>=`, `<=` on doubles. Use the helpers in `comparison_tolerance.h`: `eq()`, `lt()`, `gt()`, `le()`, `ge()`. The default epsilon is `std::numeric_limits<double>::epsilon()`, but most simulation code passes `Train::eps` explicitly. Raw comparisons cause subtle bugs where near-zero values from deceleration or integration are treated as non-zero.

### IDs are stable across rebuilds

`TrackSegment`, `TrackNode`, `Terminal`, and other rail-domain entities have integer IDs assigned at construction and preserved by the CSV input. A `RailSimMaster::reset()` rebuilds run-time state but keeps IDs identical, so saved constraints, animation paths, and external references all remain valid.

### Coroutine lifecycle

Coroutine frames are not freed automatically (`SimPromise::final_suspend = suspend_never`). They live until `Sim::reset()` clears all `SimObject`s and their handles. This is intentional — it lets the engine inspect or re-resume coroutines after their `run()` returns. Be careful when adding a `SimObject` subclass: do not capture references in `wait_until` lambdas that may not outlive the awaited condition.

## Data flow

```
input/*.csv
   │
   ▼
RailSimMaster::initialise()           builds RailNetwork from CSVs
   │
   ▼
Sim::run()                            event loop drives all coroutines
   │   ├── Train coroutines           request sections, compute motion
   │   ├── OpenLoopTrainSpawner       creates new trains at sampled times
   │   ├── SignalsManager             grants/queues section requests
   │   └── DeadlockMonitor (opt.)     detects stalls, triggers learner
   ▼
output/                               summary.csv, train_log.csv, etc.
   └── anim_script.json               (when animate=1) for Python playback
```

## Configuration

All simulation behaviour is driven by CSV files in an `input/` subdirectory of the working directory. The XLSM workbooks under `input/` are the source of truth — each contains a macro that exports its sheets to CSV, and the CSVs are gitignored. The full schema for each file is in [docs/csv_schemas/](docs/csv_schemas/).

The most-touched files:

- `runctrl.csv` — top-level simulation parameters (duration, warmup, animation toggle, seed).
- `main_option.csv` — selects the experiment mode (single replicate, throughput, deadlock avoidance).
- `network.csv` — track segments and node connectivity.
- `signals.csv` / `signal_constraints.csv` — signal placement and manual constraints.
- `open_loop_spawners.csv` — train arrival rates and routes.
- `rs_templates.csv` / `train_templates.csv` — rolling stock and train compositions.
- `passing_loops.csv` — passing-loop topology (used by `PassingLoopGroup` to auto-generate signal constraints).

## Output

- `anim_script.json` — animation events (when `animate=1` in `runctrl.csv`).
- `train_metric_log.csv` — per-train summary statistics.
- `summary_output.csv` — aggregate simulation statistics.
- `dlexp_log.csv` — deadlock-avoidance experiment iteration log (when running the deadlock-avoidance mode).
- `generated_constraints.csv` — constraints discovered by the learner.
- `warnings_log.csv` — runtime warnings, including bad CSV input rows.
