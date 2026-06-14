# CSV Input File Reference

DESLEARN reads all simulation configuration from CSV files in an `input/` subdirectory of the working directory. The XLSM workbooks under `input/` are the source of truth — each contains a macro that exports its sheets to CSV when edited. The CSVs themselves are gitignored; only the XLSMs are tracked.

This document covers every input file the simulator reads, organised by purpose.

## Conventions

- All CSVs have a header row (row 0). Data starts at row 1.
- Columns marked **optional** can be omitted from the row (or left as empty string) and will use the documented default.
- IDs are 0-based integers. Segment IDs and node IDs must each be a valid index into their respective vectors after the network is built; an out-of-range or unknown ID will throw `runtime_error` with the file name, row, and field name.
- Distances are in **kilometres**, times in **hours**, speeds in **km/h** unless otherwise noted.

---

## Contents

**Top-level control**
- [`main_option.csv`](#main_optioncsv) — selects the experiment mode
- [`runctrl.csv`](#runctrlcsv) — simulation duration, warmup, animation toggle, seed
- [`dlexp_options.csv`](#dlexp_optionscsv) — deadlock-avoidance experiment options
- [`general_params.csv`](#general_paramscsv) — physical parameters (release delay)

**Network topology**
- [`network.csv`](#networkcsv) — track segments and connectivity
- [`signals.csv`](#signalscsv) — signal placement
- [`signal_constraints.csv`](#signal_constraintscsv) — manual signal constraints
- [`passing_loops.csv`](#passing_loopscsv) — passing-loop topology
- [`speed_limits.csv`](#speed_limitscsv) — per-arc speed limits
- [`priorities.csv`](#prioritiescsv) — per-arc traversal priorities
- [`violations.csv`](#violationscsv) — segment pairs that cannot be simultaneously locked

**Trains and traffic**
- [`rs_templates.csv`](#rs_templatescsv) — rolling-stock definitions
- [`train_templates.csv`](#train_templatescsv) — train compositions
- [`open_loop_spawners.csv`](#open_loop_spawnerscsv) — train arrival rates and routes
- [`train_charts.csv`](#train_chartscsv) — predefined train traversal charts

**Animation**
- [`Paths.csv`](#pathscsv) — geographic waypoints for animation rendering

---

## Top-level control

### `main_option.csv`

Selects which experiment mode `main()` runs.

| Column | Type | Description |
|---|---|---|
| `OPTION` | string | Always literally `enter experiment type` (label only). |
| `VALUE` | string | One of: `single_replicate`, `cpu_time_exp`, `throughput_exp`, `deadlock_avoidance_exp`. |
| `COMMENT` | string | Free text. Ignored. |

For `cpu_time_exp` and `throughput_exp`, additional command-line arguments must be passed to the executable.

### `runctrl.csv`

Two-column key/value table of simulation control parameters. Recognised keys:

| Key | Type | Default | Description |
|---|---|---|---|
| `sim_len` | double (hours) | required | Total simulation horizon, including warmup. |
| `warmup` | double (hours) | required | Warmup period; statistics gathered after this point. |
| `screen_output` | int (0/1/2) | 0 | 0 = silent, 1 = per-train metric prints, 2 = full per-event log. |
| `log_output` | int (0/1/2) | 1 | 0 = none, 1 = metric log, 2 = full train log. |
| `animate` | int (0/1) | 0 | If 1, write `output/anim_script.json` for Python playback. |
| `render_cars` | int (0/1) | 0 | If 1, animate full consist (loco + cars); 0 = loco only. |
| `seed` | int | 0 | Master random seed. Used for spawner inter-arrival times. |
| `train_charts` | int (0/1) | 0 | If 1, also read `train_charts.csv`. |
| `pl_constraints` | int (0/1) | 1 | If 0, disable auto-generated passing-loop signal constraints. |
| `pl_constraint_mode` | int | 0 | Mode flag for passing-loop constraint generation. |
| `deadlock_check_interval` | double (hours) | 60.0 | How often `DeadlockMonitor` polls for stalls. Sim time. |
| `deadlock_timeout` | double (hours) | 300.0 | A request older than this is a deadlock candidate. |

Unknown keys produce a warning in `warnings_log.csv` but do not stop the simulation.

### `dlexp_options.csv`

Three-column table of deadlock-avoidance experiment options. Only read when `main_option.csv` selects `deadlock_avoidance_exp`. Recognised keys:

| Key | Type | Default | Description |
|---|---|---|---|
| `MAX_ITERATIONS` | int | 2000 | Cap on iterations per seed before declaring failure. |
| `DEFAULT_CHECK_INTERVAL` | double | 60.0 | Per-seed default for `DeadlockMonitor` interval (overrides `runctrl.csv`). |
| `DEFAULT_DEADLOCK_TIMEOUT` | double | 300.0 | Per-seed default deadlock timeout. |
| `START_DEBUG` | int | very large | Iteration index from which verbose DOT/SCC logging is emitted. |
| `WARM_START_FILE` | string | empty | If set, load constraints from this CSV before the first iteration. |
| `LAST_CONSTRAINT` | string | NA | Cutoff: when warm-starting, only load up to this constraint ID. |
| `MERGE_AND_LOWER` | int (0/1) | 1 | Enable merge-and-lower behaviour for engineered-vs-learned matches. |
| `LOG_FIRES` | int (0/1) | 0 | Write per-fire log to `output/constraint_fires.txt`. |
| `SCC_LOG` | int (0/1) | 0 | Write per-iteration SCC summaries to `output/scc_log.csv`. |
| `VERBOSE_DOT` | int (0/1) | 0 | Emit a Graphviz DOT file for the conflict graph each iteration. |
| `CONSTRAINT_EVAL` | string | flat | Constraint evaluation strategy. Currently only `flat` is supported; `tree` and `verify` are accepted with a deprecation warning. |

### `general_params.csv`

Three-column table for physical parameters not specific to any subsystem.

| Key | Type | Default | Description |
|---|---|---|---|
| `release_delay` | double (hours) | 0 | Delay between a train's tail clearing a segment and the segment becoming `FREE`. |

---

## Network topology

### `network.csv`

Defines the directed-graph structure of the rail network. One row per track segment.

| Column | Type | Description |
|---|---|---|
| `id` | int | Segment ID. Must be unique. The simulator pre-allocates `max(id)+1` segment slots, so non-contiguous IDs are tolerated but waste memory. |
| `tail` | int | Node ID at one end. |
| `head` | int | Node ID at the other end. |
| `length` | double (km) | Physical length of the segment. |
| `direction` | int | One of: `-1` (disabled), `0` (forward only — tail→head), `1` (bidirectional), `2` (reverse only — head→tail; `tail`/`head` columns are swapped at load time). |
| `comment` | string | Free text. Stored as `descr_1`. |
| `comment 2` | string | Free text. Stored as `descr_2`; conventionally used to mark spawn/despawn arcs. |

### `signals.csv`

Marks which directed arcs carry signals, and which terminate at terminals.

| Column | Type | Description |
|---|---|---|
| `segment` | int | Segment ID this signal lives on. |
| `head` | int | Head node ID — selects which directed arc of the segment (forward vs reverse). |
| `margin` | double (km) | Stopping margin: the signal's effective stop position is `margin` short of the head node. |
| `lock_thru` | int (0/1) | If 1, this is a lock-through signal — trains can lock past it without stopping (typical for passing-loop mainlines). |
| `terminal` | string | Terminal name if this arc terminates at a terminal, otherwise `0` or empty. New terminals are created as encountered. |
| `comment` | string | Free text. |

### `signal_constraints.csv`

Manual signal constraints (occupancy limits) defined by the modeller. Each constraint has a header row followed by one row per arc the constraint covers.

**Header row** (where `head` column is empty):
| Column | Type | Description |
|---|---|---|
| `str_id` | string | Constraint ID. Must be unique. |
| `segment / occ_limit` | int | Occupancy limit for this constraint. |

**Arc row** (where `head` column is non-empty, `str_id` matches the most recent header):
| Column | Type | Description |
|---|---|---|
| `str_id` | string | Same as the header above. |
| `segment / occ_limit` | int | Segment ID. |
| `head` | int | Head node ID. |
| `target` | string | Terminal name to qualify the constraint, or `NA` to apply to all targets. |
| `comment` | string | Free text. |

A constraint is violated when more than `occ_limit` distinct trains are occupying or requesting any of its arcs (subject to target matching).

### `passing_loops.csv`

Defines passing-loop topology. The `PassingLoopGroup` consumer auto-generates the standard set of passing-loop signal constraints from these rows.

| Column | Type | Description |
|---|---|---|
| `pl_id` | int | Passing-loop group ID. Multiple rows with the same ID describe one passing loop. |
| `segment` | int | Segment ID. |
| `head` | int | Head node ID — selects which directed arc. |
| `margin` | double (km) | Stop margin (see `signals.csv`). |
| `opp_speed_lim` | double (km/h) | Speed limit imposed on opposing-direction traffic at this arc. |
| `arc_type` | string | One of: `APPR0`, `APPR1` (approach signals), `ML0`, `ML1` (mainlines), `S0`, `S1` (sidings). |
| `max_length` | double (km) — optional | If present (column 6 of an 8+ column row), maximum train length permitted on this arc. Used to model siding overhang for overlength trains. |
| `comment` | string | Free text. |

### `speed_limits.csv`

Per-arc speed limits.

| Column | Type | Description |
|---|---|---|
| `segment` | int | Segment ID. |
| `head` | int | Head node ID. |
| `speed_limit` | double (km/h) | Maximum speed on this arc. |
| `comment` | string | Free text. |

### `priorities.csv`

Per-arc traversal priorities. Higher priority arcs are preferred when the route-finder has alternatives.

| Column | Type | Description |
|---|---|---|
| `segment` | int | Segment ID. |
| `head` | int | Head node ID. |
| `priority` | double | Priority value. Higher is preferred. |
| `comment` | string | Free text. |

### `violations.csv`

Segment pairs that cannot be simultaneously locked. Used to encode physical conflicts (e.g., a single switch shared by two segments).

| Column | Type | Description |
|---|---|---|
| `segment1` | int | First segment ID. |
| `segment2` | int | Second segment ID. |

The relation is symmetric — pairs are typically listed both ways.

---

## Trains and traffic

### `rs_templates.csv`

Rolling-stock templates: locomotives, cars, etc.

| Column | Type | Description |
|---|---|---|
| `name` | string | Template name (referenced from `train_templates.csv`). |
| `length` | double (km) | Length of one unit. |
| `sprite_file` | string | PNG filename for animation rendering. |
| `sprite_w` | double (km) | Sprite width in true-scale units. |
| `sprite_h` | double (km) | Sprite height in true-scale units. |
| `sprite_scale` | double | Scale factor for animation rendering. |
| `r`, `g`, `b` | int (0–255) — optional | Colour override. Default 255/255/255 (white). |

### `train_templates.csv`

Train compositions — sequences of rolling-stock units that form a complete train.

| Column | Type | Description |
|---|---|---|
| `name` | string | Train template name (referenced from spawners). |
| `max_spd` | double (km/h) | Maximum speed. |
| `accel` | double (km/h²) | Maximum acceleration. |
| `decel` | double (km/h²) | Maximum deceleration. |
| `set0_n`, `set0_rst` | int, string | First rolling-stock set: count and `rs_templates.csv` name. |
| `set1_n`, `set1_rst` | int, string | Second rolling-stock set. |
| ... | ... | Up to 5 sets (`set0` through `set4`). Empty cells terminate. |

A `freight` train with `set0_n=2, set0_rst=loco, set1_n=20, set1_rst=car` is 2 locomotives followed by 20 cars.

### `open_loop_spawners.csv`

Defines train arrival processes. Each row creates one spawner that emits trains at intervals drawn from a configured distribution.

| Column | Type | Description |
|---|---|---|
| `name` | string | Spawner name. |
| `spawn_seg` | int | Segment ID where trains appear. |
| `spawn_head` | int | Head node ID — selects directed arc on the spawn segment. |
| `train_template` | string | Name from `train_templates.csv`. |
| `distribution` | string | One of: `uniform`, `normal`, `exponential`. |
| `dist_prm1` | double | First distribution parameter. For `exponential`: rate (trains/hr). For `normal`: mean. For `uniform`: lower bound. |
| `dist_prm2` | double | Second parameter. For `exponential`: ignored. For `normal`: stddev. For `uniform`: upper bound. |
| `terminal1`, `dwell1` | string, double (hr) | First task: terminal name (or `NA` to skip), dwell time. |
| `terminal2`, `dwell2` | string, double (hr) | Second task. |
| ... | ... | Continue with additional terminal/dwell pairs as needed. |

The `NA` terminal sentinel is used for transit-only legs where the train passes through without stopping.

### `train_charts.csv`

Optional. Predefined per-train chart data — used for replaying recorded train movements rather than generating them stochastically. Read only when `runctrl.csv:train_charts` is non-zero. See `read_train_charts()` in `rail_sim_tools.cpp` for the row format (segment, head, exit-node list terminated by `END`, entry-arc list terminated by `END`).

---

## Animation

### `Paths.csv`

Geographic waypoints for animation rendering. Generated by the simulator (`Paths.csv` is *not* a hand-edited input; it is exported by the C++ simulator alongside `anim_script.json`).

| Column | Type | Description |
|---|---|---|
| `path_id` | string | Path identifier in format `(seg_id-head_node_id)`. |
| `waypt_x` | double | External x-coordinate (typically UTM easting in metres). |
| `waypt_y` | double | External y-coordinate (typically UTM northing in metres). |
| `line_id` | int | Optional line identifier (for multi-line networks). |
| `warnings` | string | Optional warning text from path generation. |

Consecutive rows with the same `path_id` form one path; consecutive waypoints become `PathSegment`s in `DesVizPath`.
