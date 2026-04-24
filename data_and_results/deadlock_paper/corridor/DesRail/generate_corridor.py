"""
Generate input CSV files for a synthetic single-track corridor with N passing loops.

Topology:
    W_spawn -> W_junction -> [corridor] -> PL_0 -> [corridor] -> PL_1 -> ... -> PL_{N-1} -> [corridor] -> E_junction -> E_spawn

Each passing loop has:
    - Two junction nodes (west and east)
    - A main line segment between them (bidirectional)
    - A siding segment between them (bidirectional, reversed tail/head for parallel paths)

Terminals:
    - West_entry / West_exit at W_junction
    - East_entry / East_exit at E_junction

Usage:
    python generate_corridor.py --num_loops N [--output_dir DIR] [--corridor_len L]
                                [--loop_len L] [--spawn_rate R] [--horizon T] [--seed S]
"""

import argparse
import os
import csv
import random


def generate_corridor(num_loops, output_dir, corridor_len, loop_len, spawn_rate, horizon, seed,
                      corridor_len_var=0.0, warmup=0):
    input_dir = os.path.join(output_dir, "input")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, "output"), exist_ok=True)

    # ========== NODE LAYOUT ==========
    # Node 0: West spawn point
    # Node 1: West junction (terminal signal node)
    # Node 2+2i: PL_i west junction  (i = 0 .. N-1)
    # Node 3+2i: PL_i east junction  (i = 0 .. N-1)
    # Node 2N+2: East junction (terminal signal node)
    # Node 2N+3: East spawn point
    N = num_loops
    node_W_spawn = 0
    node_W_junc = 1
    def node_pl_west(i): return 2 + 2 * i
    def node_pl_east(i): return 3 + 2 * i
    node_E_junc = 2 * N + 2
    node_E_spawn = 2 * N + 3
    num_nodes = 2 * N + 4

    # ========== CORRIDOR LENGTH SAMPLING ==========
    # If corridor_len_var > 0, sample each corridor segment length from
    # uniform(corridor_len - corridor_len_var, corridor_len + corridor_len_var)
    rng = random.Random(seed)  # dedicated RNG so simulation seed is unaffected
    def sample_corridor_len():
        if corridor_len_var > 0:
            return round(rng.uniform(corridor_len - corridor_len_var, corridor_len + corridor_len_var), 3)
        return corridor_len

    # ========== SEGMENT LAYOUT ==========
    segments = []  # (id, tail, head, length, direction)
    seg_id = 0

    # West terminal: spawn and exit segments (direction=0)
    seg_W_spawn = seg_id; segments.append((seg_id, node_W_spawn, node_W_junc, 1.0, 0, "West spawn")); seg_id += 1
    seg_W_exit  = seg_id; segments.append((seg_id, node_W_junc, node_W_spawn, 0.5, 0, "West exit")); seg_id += 1

    # West lead-in: corridor from W_junction to first PL (or to E_junction if N=0)
    if N > 0:
        seg_W_corridor = seg_id
        segments.append((seg_id, node_W_junc, node_pl_west(0), sample_corridor_len(), 1, "West corridor to PL_0"))
        seg_id += 1
    else:
        seg_W_corridor = seg_id
        segments.append((seg_id, node_W_junc, node_E_junc, sample_corridor_len(), 1, "Direct corridor W to E"))
        seg_id += 1

    # Passing loops and inter-loop corridors
    seg_pl_main = []  # seg id for each PL main line
    seg_pl_side = []  # seg id for each PL siding
    seg_corridors = []  # corridor segments between PLs

    for i in range(N):
        w = node_pl_west(i)
        e = node_pl_east(i)
        # Main line: tail=east, head=west (reversed, like Toowoomba's seg 26)
        seg_main = seg_id
        segments.append((seg_id, e, w, loop_len, 1, f"PL_{i} main line"))
        seg_id += 1
        seg_pl_main.append(seg_main)

        # Siding: tail=west, head=east (normal direction, like Toowoomba's seg 25)
        seg_side = seg_id
        segments.append((seg_id, w, e, loop_len, 1, f"PL_{i} siding"))
        seg_id += 1
        seg_pl_side.append(seg_side)

        # Corridor to next PL (or to E_junction)
        if i < N - 1:
            seg_corr = seg_id
            segments.append((seg_id, node_pl_east(i), node_pl_west(i + 1), sample_corridor_len(), 1,
                             f"Corridor PL_{i} to PL_{i+1}"))
            seg_id += 1
            seg_corridors.append(seg_corr)
        else:
            seg_corr = seg_id
            segments.append((seg_id, node_pl_east(i), node_E_junc, sample_corridor_len(), 1,
                             f"Corridor PL_{i} to East"))
            seg_id += 1
            seg_corridors.append(seg_corr)

    # East terminal: spawn and exit segments (direction=0)
    seg_E_spawn = seg_id; segments.append((seg_id, node_E_spawn, node_E_junc, 1.0, 0, "East spawn")); seg_id += 1
    seg_E_exit  = seg_id; segments.append((seg_id, node_E_junc, node_E_spawn, 0.5, 0, "East exit")); seg_id += 1

    # ========== WRITE network.csv ==========
    with open(os.path.join(input_dir, "network.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "tail", "head", "length", "direction", "comment", "comment 2"])
        for s in segments:
            w.writerow([s[0], s[1], s[2], f"{s[3]:.6f}", s[4], s[5], ""])

    # ========== SIGNALS ==========
    # Signals define where trains stop/start and terminals
    signals = []  # (segment, head, margin, lock_thru, terminal)

    # West terminal signals
    signals.append((seg_W_spawn, node_W_junc, 0.05, 0, "West_entry", "West spawn entry"))
    signals.append((seg_W_exit, node_W_spawn, 0.0, 0, "West_exit", "West exit terminal"))

    # East terminal signals
    signals.append((seg_E_spawn, node_E_junc, 0.05, 0, "East_entry", "East spawn entry"))
    signals.append((seg_E_exit, node_E_spawn, 0.0, 0, "East_exit", "East exit terminal"))

    # Terminal approach signals (lock_thru=1) — create section boundaries between
    # PLs and terminal corridors so trains don't lock the corridor when entering the PL.
    # Without these, sections extend from the PL through the corridor to the terminal,
    # preventing the deadlock scenario where opposing trains block each other.
    if N > 0:
        # West corridor: westbound arc (toward W_junc) needs a signal
        signals.append((seg_W_corridor, node_W_junc, 0.05, 0, "", "West corridor approach"))
        # East corridor: eastbound arc (toward E_junc) needs a signal
        east_corridor_seg = seg_corridors[-1]  # last corridor segment
        signals.append((east_corridor_seg, node_E_junc, 0.05, 0, "", "East corridor approach"))

    with open(os.path.join(input_dir, "signals.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["segment", "head", "margin", "lock_thru", "terminal", "comment"])
        for s in signals:
            w.writerow(list(s))

    # ========== PASSING LOOPS ==========
    # Each PL has 6 arcs: APPR0, APPR1, ML0, ML1, S0, S1
    # Following the Toowoomba pattern:
    #   Direction 0 = westbound (toward west terminal)
    #   Direction 1 = eastbound (toward east terminal)
    passing_loops = []

    for i in range(N):
        pw = node_pl_west(i)
        pe = node_pl_east(i)
        main_seg = seg_pl_main[i]  # tail=east, head=west
        side_seg = seg_pl_side[i]  # tail=west, head=east

        # Approach segments:
        if i == 0:
            appr_west_seg = seg_W_corridor  # corridor from W_junc to PL_0_west
        else:
            appr_west_seg = seg_corridors[i - 1]  # corridor from PL_{i-1}_east to PL_i_west

        appr_east_seg = seg_corridors[i]  # corridor from PL_i_east to next

        # Following Rangeview pattern: APPR0 arrives at south/west junction, APPR1 at north/east.
        # APPR0: approach from west side, arriving at pw (feeds into ML0/S0 = eastbound traversal)
        passing_loops.append((i, appr_west_seg, pw, 0.05, 40, "APPR0", f"PL_{i} appr from west"))

        # APPR1: approach from east side, arriving at pe (feeds into ML1/S1 = westbound traversal)
        passing_loops.append((i, appr_east_seg, pe, 0.05, 40, "APPR1", f"PL_{i} appr from east"))

        # Main line: tail=pe, head=pw (reversed, like Rangeview seg 26 tail=21,head=20)
        # ML1: forward arc (main, pw) = from pe toward pw = westbound
        # ML0: reverse arc (main, pe) = from pw toward pe = eastbound
        passing_loops.append((i, main_seg, pw, 0.05, 0, "ML1", f"PL_{i} ML WB"))
        passing_loops.append((i, main_seg, pe, 0.05, 0, "ML0", f"PL_{i} ML EB"))

        # Siding: tail=pw, head=pe (normal, like Rangeview seg 25 tail=20,head=21)
        # S1: reverse arc (side, pw) = from pe toward pw = westbound
        # S0: forward arc (side, pe) = from pw toward pe = eastbound
        passing_loops.append((i, side_seg, pw, 0.05, 20, "S1", f"PL_{i} siding WB"))
        passing_loops.append((i, side_seg, pe, 0.05, 20, "S0", f"PL_{i} siding EB"))

    with open(os.path.join(input_dir, "passing_loops.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pl_id", "segment", "head", "margin", "opp_speed_lim", "arc_type", "comment"])
        for pl in passing_loops:
            w.writerow(list(pl))

    # ========== VIOLATIONS ==========
    # At each PL, the main line and siding are parallel paths - add geometry violation
    violations = []
    for i in range(N):
        violations.append((seg_pl_main[i], seg_pl_side[i]))
        violations.append((seg_pl_side[i], seg_pl_main[i]))

    # Terminal spawn/exit segments share the same physical track (opposite directions)
    violations.append((seg_W_spawn, seg_W_exit))
    violations.append((seg_W_exit, seg_W_spawn))
    violations.append((seg_E_spawn, seg_E_exit))
    violations.append((seg_E_exit, seg_E_spawn))

    with open(os.path.join(input_dir, "violations.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["segment1", "segment2"])
        for v in violations:
            w.writerow(list(v))

    # ========== SPEED LIMITS ==========
    # Keep it simple: no additional speed limits beyond passing loop opp_speeds
    with open(os.path.join(input_dir, "speed_limits.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["segment", "head", "speed_limit", "comment"])

    # ========== PRIORITIES ==========
    with open(os.path.join(input_dir, "priorities.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["segment", "head", "priority", "comment"])

    # ========== RS TEMPLATES ==========
    with open(os.path.join(input_dir, "rs_templates.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "length", "sprite_file", "sprite_w", "sprite_h", "sprite_scale"])
        w.writerow(["loco", 0.02, "train_blue.png", 50, 100, 0.2])
        w.writerow(["car", 0.02, "car.png", 25, 50, 0.1])

    # ========== TRAIN TEMPLATES ==========
    with open(os.path.join(input_dir, "train_templates.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "max_spd", "accel", "decel",
                     "set0_n", "set0_rst", "set1_n", "set1_rst",
                     "set2_n", "set2_rst", "set3_n", "set3_rst",
                     "set4_n", "set4_rst"])
        w.writerow(["freight", 80, 360, 720, 2, "loco", 20, "car",
                     "", "", "", "", "", ""])

    # ========== OPEN LOOP SPAWNERS ==========
    # Bidirectional traffic: spawn at both ends
    spawners = []
    # Eastbound: spawn at West, destination East_exit
    spawners.append(("ols_EB", seg_W_spawn, node_W_junc, "freight", "exponential",
                     spawn_rate, 0, "NA", 0, "East_exit", 0))
    # Westbound: spawn at East, destination West_exit
    spawners.append(("ols_WB", seg_E_spawn, node_E_junc, "freight", "exponential",
                     spawn_rate, 0, "NA", 0, "West_exit", 0))

    with open(os.path.join(input_dir, "open_loop_spawners.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "spawn_seg", "spawn_head", "train_template", "distribution",
                     "dist_prm1", "dist_prm2", "terminal1", "dwell1", "terminal2", "dwell2"])
        for s in spawners:
            w.writerow(list(s))

    # ========== GENERAL PARAMS ==========
    with open(os.path.join(input_dir, "general_params.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["parameter", "value", "comment"])
        w.writerow(["release_delay", 0.016666667, "hours"])

    # ========== RUNCTRL ==========
    with open(os.path.join(input_dir, "runctrl.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["OPTION", "VALUE", ""])
        w.writerow(["sim_len", horizon, "hours"])
        w.writerow(["warmup", warmup, "hours"])
        w.writerow(["screen_output", 0, ""])
        w.writerow(["log_output", 0, ""])
        w.writerow(["animate", 0, ""])
        w.writerow(["train_charts", 0, ""])
        w.writerow(["pl_constraints", 0, ""])
        w.writerow(["seed", seed, "random seed"])

    # ========== MAIN OPTION ==========
    with open(os.path.join(input_dir, "main_option.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["OPTION", "VALUE", "COMMENT"])
        w.writerow(["enter experiment type", "deadlock_avoidance_exp", ""])

    # ========== SIGNAL CONSTRAINTS (empty) ==========
    with open(os.path.join(input_dir, "signal_constraints.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["str_id", "segment / occ_limit", "head", "target", "comment"])

    # ========== DLEXP OPTIONS ==========
    with open(os.path.join(input_dir, "dlexp_options.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["OPTION", "VALUE", "COMMENT"])
        w.writerow(["MAX_ITERATIONS", 2000, ""])
        w.writerow(["DEFAULT_CHECK_INTERVAL", 1, ""])
        w.writerow(["DEFAULT_DEADLOCK_TIMEOUT", 2, ""])
        w.writerow(["START_DEBUG", 2001, ""])
        w.writerow(["WARM_START_FILE", "", ""])
        w.writerow(["LAST_CONSTRAINT", "", ""])
        w.writerow(["CONSTRAINT_EVAL", "tree", ""])

    # ========== TRAIN CHARTS (empty) ==========
    with open(os.path.join(input_dir, "train_charts.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id(0-1-2-...)", "name", "start seg", "start head",
                     "list exit nodes (finish list with END), followed by entry arcs"])

    # ========== PATHS (empty — not needed for simulation) ==========
    with open(os.path.join(input_dir, "Paths.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path_id", "waypt_x", "waypt_y", "line_id", "warnings"])

    # ========== SUMMARY ==========
    print(f"Generated corridor with {N} passing loops:")
    print(f"  Nodes: {num_nodes}")
    print(f"  Segments: {seg_id}")
    if corridor_len_var > 0:
        print(f"  Corridor length: U({corridor_len - corridor_len_var}, {corridor_len + corridor_len_var}) km between each PL")
    else:
        print(f"  Corridor length: {corridor_len} km between each PL")
    print(f"  Loop length: {loop_len} km (main + siding)")
    print(f"  Spawn rate: exponential({spawn_rate}) from each end")
    print(f"  Horizon: {horizon} hours (warmup: {warmup} hours)")
    print(f"  Seed: {seed}")
    print(f"  Output: {output_dir}")

    # Print network diagram (look up actual corridor lengths from segments list)
    corridor_seg_ids = [seg_W_corridor] + seg_corridors
    corridor_lengths = {s[0]: s[3] for s in segments}
    print(f"\n  Network topology:")
    parts = ["W_spawn(0)--W_junc(1)"]
    for i in range(N):
        clen = corridor_lengths[corridor_seg_ids[i]]
        parts.append(f"--[{clen}km]--PL{i}({node_pl_west(i)},{node_pl_east(i)})")
    if N > 0:
        clen = corridor_lengths[corridor_seg_ids[N]]
    else:
        clen = corridor_lengths[seg_W_corridor]
    parts.append(f"--[{clen}km]--E_junc({node_E_junc})--E_spawn({node_E_spawn})")
    print("  " + "".join(parts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic single-track corridor for DesRail")
    parser.add_argument("--num_loops", "-n", type=int, required=True,
                        help="Number of passing loops (2-10 typical)")
    parser.add_argument("--output_dir", "-o", type=str, default=None,
                        help="Output directory (default: input_corridor_N)")
    parser.add_argument("--corridor_len", type=float, default=5.0,
                        help="Mean length of single-track corridor between PLs in km (default: 5.0)")
    parser.add_argument("--corridor_len_var", type=float, default=0.0,
                        help="Half-width of uniform variation around corridor_len in km (default: 0.0, i.e. fixed)")
    parser.add_argument("--loop_len", type=float, default=0.7,
                        help="Length of passing loop main/siding in km (default: 0.7)")
    parser.add_argument("--spawn_rate", type=float, default=0.8,
                        help="Mean inter-arrival time (exponential) in hours (default: 0.8)")
    parser.add_argument("--horizon", type=float, default=192,
                        help="Simulation horizon (sim_len) in hours (default: 192)")
    parser.add_argument("--warmup", type=float, default=24,
                        help="Warmup period in hours (default: 24)")
    parser.add_argument("--seed", type=int, default=1,
                        help="Random seed (default: 1)")

    args = parser.parse_args()

    if args.output_dir is None:
        args.output_dir = f"./input_corridor_{args.num_loops}"

    generate_corridor(
        num_loops=args.num_loops,
        output_dir=args.output_dir,
        corridor_len=args.corridor_len,
        loop_len=args.loop_len,
        spawn_rate=args.spawn_rate,
        horizon=args.horizon,
        seed=args.seed,
        corridor_len_var=args.corridor_len_var,
        warmup=args.warmup,
    )
