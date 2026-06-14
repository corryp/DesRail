"""
Set up a corridor configuration in DesRail/input/ for animation debugging.

Generates an N-loop corridor with overlength train support, configures it for
a single animated simulation run, and backs up existing input files.

Usage:
    python setup_corridor_anim.py                    # N=5, defaults
    python setup_corridor_anim.py --num_loops 3      # N=3
    python setup_corridor_anim.py --rate 2.5 --frac 0.50  # heavier traffic, more overlength
    python setup_corridor_anim.py --restore           # restore backed-up input files
    python setup_corridor_anim.py --sim_len 12        # shorter sim for quick test
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(SCRIPT_DIR, "input")
BACKUP_DIR = os.path.join(SCRIPT_DIR, "input_backup_anim")
TEMP_DIR = os.path.join(SCRIPT_DIR, "_corridor_temp")
GENERATOR_SCRIPT = os.path.join(SCRIPT_DIR, "generate_corridor.py")
CONSTRAINT_GEN_SCRIPT = os.path.join(SCRIPT_DIR, "generate_overlength_constraints.py")


def set_csv_option(filepath, option_name, value):
    """Set a value in a CSV config file where column 0 is the option name."""
    rows = []
    found = False
    with open(filepath, "r", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if row and row[0] == option_name:
                row[1] = str(value)
                found = True
            rows.append(row)
    if not found:
        raise ValueError(f"Option '{option_name}' not found in {filepath}")
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


def write_overlength_spawners(filepath, rate, fraction, num_loops):
    """Adjust spawner rates for the desired short/long traffic split.

    With --overlength, generate_corridor.py creates 4 base spawners with
    separate spawn segments for short and long trains. This function just
    sets the rates based on the desired fraction.
    """
    # Read base spawners (already have correct segments and train templates)
    header = None
    base_rows = []
    with open(filepath, "r", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        for row in reader:
            if row:
                base_rows.append(row)

    # Set rates: short spawners get rate*(1-fraction), long get rate*fraction
    for row in base_rows:
        template = row[3]  # train_template column
        if "long" in template.lower():
            row[5] = str(rate * fraction) if fraction > 0 else str(0)
        else:
            row[5] = str(rate * (1.0 - fraction)) if fraction < 1.0 else str(0)

    # Remove spawners with zero rate
    base_rows = [r for r in base_rows if float(r[5]) > 0]

    with open(filepath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in base_rows:
            w.writerow(row)


def generate_schematic_paths(num_loops, network_csv_path, output_path):
    """Generate Paths.csv with schematic waypoints for a corridor.

    Reads network.csv directly to get the actual segment topology, so arc IDs
    are guaranteed to match what the C++ simulator produces.

    Layout: horizontal corridor with equal-spaced nodes, sidings offset vertically.
    Schematic coordinates use arbitrary units — the animation auto-scales.
    """
    N = num_loops
    SEG_LEN = 100       # schematic length per corridor/PL segment
    SIDING_Y = 15       # vertical offset for sidings
    SPAWN_LEN = 60      # length of spawn/exit stubs

    # Read network.csv to get segment definitions
    segments = []  # list of (id, tail, head, direction)
    with open(network_csv_path, "r", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            if row:
                seg_id = int(row[0])
                tail = int(row[1])
                head = int(row[2])
                direction = int(row[4])
                comment = row[5] if len(row) > 5 else ""
                segments.append((seg_id, tail, head, direction, comment))

    # Identify siding segments (comment contains "siding")
    siding_segs = set()
    for seg_id, tail, head, direction, comment in segments:
        if "siding" in comment.lower():
            siding_segs.add(seg_id)

    # Node positions — lay out horizontally with equal spacing
    node_x = {}
    node_y = {}
    node_x[0] = 0
    node_y[0] = 0
    node_x[1] = SPAWN_LEN
    node_y[1] = 0

    x_cursor = SPAWN_LEN + SEG_LEN
    for i in range(N):
        pw = 2 + 2 * i
        pe = 3 + 2 * i
        node_x[pw] = x_cursor
        node_y[pw] = 0
        node_x[pe] = x_cursor + SEG_LEN
        node_y[pe] = 0
        x_cursor = node_x[pe] + SEG_LEN

    node_E_junc = 2 * N + 2
    node_E_spawn = 2 * N + 3
    node_x[node_E_junc] = x_cursor
    node_y[node_E_junc] = 0
    node_x[node_E_spawn] = x_cursor + SPAWN_LEN
    node_y[node_E_spawn] = 0

    # Long spawn nodes (if present) — offset below mainline near their junction
    LONG_SPAWN_Y = -SIDING_Y
    for seg_id, csv_tail, csv_head, direction, comment in segments:
        if "spawn long" in comment.lower():
            # spawn seg: tail=spawn_node, head=junction
            spawn_node = csv_tail
            junc_node = csv_head
            if "west" in comment.lower():
                node_x[spawn_node] = node_x[junc_node] - SPAWN_LEN
            else:
                node_x[spawn_node] = node_x[junc_node] + SPAWN_LEN
            node_y[spawn_node] = LONG_SPAWN_Y

    # Build arc waypoints matching C++ arc creation logic:
    #   direction <= 1: node1=csv_tail, node2=csv_head
    #   direction == 2: node1=csv_head, node2=csv_tail  (reversed)
    #   forward_arc: head=node2  (always created unless disabled)
    #   reverse_arc: head=node1  (only if bidirectional, i.e. direction==1)
    arcs = []  # list of (path_id, [(x,y), ...])

    for seg_id, csv_tail, csv_head, direction, comment in segments:
        if direction == -1:
            continue  # disabled segment

        # C++ node assignment
        if direction <= 1:
            node1, node2 = csv_tail, csv_head
        else:
            node1, node2 = csv_head, csv_tail

        is_siding = seg_id in siding_segs
        is_bidi = (direction == 1)

        # Forward arc: node1 → node2 (head = node2)
        if is_siding:
            # 45-degree turnouts: offset horizontally by SIDING_Y at each end
            x0, x1 = node_x[node1], node_x[node2]
            arcs.append((f"({seg_id}-{node2})", [
                (x0, 0), (x0 + SIDING_Y, SIDING_Y),
                (x1 - SIDING_Y, SIDING_Y), (x1, 0)
            ]))
        else:
            arcs.append((f"({seg_id}-{node2})", [
                (node_x[node1], node_y[node1]),
                (node_x[node2], node_y[node2]),
            ]))

        # Reverse arc: node2 → node1 (head = node1), only if bidirectional
        if is_bidi:
            if is_siding:
                x0, x1 = node_x[node2], node_x[node1]
                arcs.append((f"({seg_id}-{node1})", [
                    (x0, 0), (x0 - SIDING_Y, SIDING_Y),
                    (x1 + SIDING_Y, SIDING_Y), (x1, 0)
                ]))
            else:
                arcs.append((f"({seg_id}-{node1})", [
                    (node_x[node2], node_y[node2]),
                    (node_x[node1], node_y[node1]),
                ]))

    # Write Paths.csv
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path_id", "waypt_x", "waypt_y", "line_id", "warnings"])
        for path_id, waypoints in arcs:
            for x, y in waypoints:
                w.writerow([path_id, f"{x:.1f}", f"{y:.1f}", "", ""])

    print(f"Generated schematic Paths.csv: {len(arcs)} arcs, "
          f"{sum(len(wp) for _, wp in arcs)} waypoints")


def backup_input():
    """Back up current input/ files (excluding .xlsm) to input_backup_anim/."""
    if os.path.exists(BACKUP_DIR):
        print(f"Backup already exists at {BACKUP_DIR} — skipping backup")
        return
    os.makedirs(BACKUP_DIR)
    for fname in os.listdir(INPUT_DIR):
        if fname.endswith(".xlsm") or fname.startswith("~$"):
            continue
        src = os.path.join(INPUT_DIR, fname)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(BACKUP_DIR, fname))
    print(f"Backed up {len(os.listdir(BACKUP_DIR))} files to {BACKUP_DIR}")


def restore_input():
    """Restore input/ from backup."""
    if not os.path.exists(BACKUP_DIR):
        print("No backup found — nothing to restore")
        return
    for fname in os.listdir(BACKUP_DIR):
        src = os.path.join(BACKUP_DIR, fname)
        dst = os.path.join(INPUT_DIR, fname)
        shutil.copy2(src, dst)
    # Clean up corridor-specific files that weren't in the backup
    backup_files = set(os.listdir(BACKUP_DIR))
    for fname in os.listdir(INPUT_DIR):
        if fname.endswith(".xlsm") or fname.startswith("~$"):
            continue
        if fname not in backup_files:
            os.remove(os.path.join(INPUT_DIR, fname))
            print(f"  Removed corridor file: {fname}")
    shutil.rmtree(BACKUP_DIR)
    print("Restored input/ from backup")


def setup(args):
    """Generate corridor configs and install into input/."""
    N = args.num_loops

    # 1. Back up current input
    backup_input()

    # 2. Generate corridor configs to temp dir
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)

    cmd = [
        sys.executable, GENERATOR_SCRIPT,
        "--num_loops", str(N),
        "--output_dir", TEMP_DIR,
        "--corridor_len", str(args.corridor_len),
        "--corridor_len_var", str(args.corridor_len_var),
        "--loop_len", str(args.loop_len),
        "--spawn_rate", str(args.rate),  # placeholder, overwritten below
        "--horizon", str(args.sim_len),
        "--warmup", "0",
        "--seed", str(args.seed),
        "--overlength",
        "--pl_max_length", str(args.pl_max_length),
    ]
    print(f"Generating N={N} corridor...")
    subprocess.run(cmd, check=True)

    # 3. Copy generated configs into input/ (preserve .xlsm files)
    temp_input = os.path.join(TEMP_DIR, "input")
    for fname in os.listdir(temp_input):
        src = os.path.join(temp_input, fname)
        dst = os.path.join(INPUT_DIR, fname)
        shutil.copy2(src, dst)

    # 4. Generate overlength constraints
    nogood_path = os.path.join(INPUT_DIR, "nogood_constr.csv")
    print(f"Generating overlength constraints...")
    subprocess.run([
        sys.executable, CONSTRAINT_GEN_SCRIPT,
        "-n", str(N), "-o", nogood_path,
        "--min_length", str(args.min_length),
    ], check=True)

    # 5. Rewrite rs_templates and train_templates for coloured sprites
    #    Loco: red, short wagon: green, long wagon: blue — all use train_white.png
    rs_path = os.path.join(INPUT_DIR, "rs_templates.csv")
    with open(rs_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "length", "sprite_file", "sprite_w", "sprite_h",
                     "sprite_scale", "r", "g", "b"])
        w.writerow(["loco",      0.02, "train_white.png", 50, 100, 0.2, 220, 40,  40])   # red
        w.writerow(["car_short", 0.02, "train_white.png", 25, 50,  0.1, 40,  180, 40])   # green
        w.writerow(["car_long",  0.02, "train_white.png", 25, 50,  0.1, 40,  80,  220])  # blue

    tt_path = os.path.join(INPUT_DIR, "train_templates.csv")
    with open(tt_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "max_spd", "accel", "decel",
                     "set0_n", "set0_rst", "set1_n", "set1_rst",
                     "set2_n", "set2_rst", "set3_n", "set3_rst",
                     "set4_n", "set4_rst"])
        w.writerow(["freight",      80, 360, 720, 2, "loco", 20, "car_short",
                     "", "", "", "", "", ""])
        w.writerow(["freight_long", 80, 360, 720, 2, "loco", 38, "car_long",
                     "", "", "", "", "", ""])

    # 6. Configure for animated single run
    runctrl = os.path.join(INPUT_DIR, "runctrl.csv")
    set_csv_option(runctrl, "animate", 1)
    set_csv_option(runctrl, "sim_len", args.sim_len)
    set_csv_option(runctrl, "screen_output", 1)
    set_csv_option(runctrl, "pl_constraints", 1)
    set_csv_option(runctrl, "pl_constraint_mode", 1)

    # Add render_cars if not present (generate_corridor.py doesn't include it)
    with open(runctrl, "r", newline="") as f:
        content = f.read()
    if "render_cars" not in content:
        with open(runctrl, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["render_cars", 1, "(0 = loco only | 1 = full consist)"])

    # 6. Use deadlock_avoidance_exp with MAX_ITERATIONS=1
    #    This loads nogood_constr.csv automatically and runs one simulation
    dlexp = os.path.join(INPUT_DIR, "dlexp_options.csv")
    set_csv_option(dlexp, "MAX_ITERATIONS", 1)
    set_csv_option(dlexp, "START_DEBUG", 0)  # enable animation/debug on iteration 0
    set_csv_option(dlexp, "CONSTRAINT_EVAL", "flat")  # flat avoids tree eval bug
    set_csv_option(dlexp, "LOG_CONSTRAINT_FIRES", 1)

    # 7. Write overlength spawners
    spawner_path = os.path.join(INPUT_DIR, "open_loop_spawners.csv")
    write_overlength_spawners(spawner_path, args.rate, args.frac, N)

    # 8. Generate schematic Paths.csv (reads network.csv for exact arc IDs)
    paths_path = os.path.join(INPUT_DIR, "Paths.csv")
    network_csv = os.path.join(INPUT_DIR, "network.csv")
    generate_schematic_paths(N, network_csv, paths_path)

    # Clean up temp
    shutil.rmtree(TEMP_DIR)

    # Summary
    print(f"\n{'='*60}")
    print(f"Corridor N={N} configured for animation debugging:")
    print(f"  Rate: {args.rate} hr  |  Overlength fraction: {args.frac}")
    print(f"  Sim length: {args.sim_len} hr  |  Seed: {args.seed}")
    print(f"  PL constraints: ON (segment mode)")
    print(f"  Overlength constraints: nogood_constr.csv")
    print(f"  Animation: ON")
    print(f"  Experiment: deadlock_avoidance_exp (1 iteration)")
    print(f"  Constraint eval: flat")
    print(f"{'='*60}")
    print(f"\nTo run:")
    print(f"  cd DesRail && ../x64/Release/DesRail.exe")
    print(f"  cd ../Animation && python DesRailAnim.py")
    print(f"\nTo restore original input:")
    print(f"  python setup_corridor_anim.py --restore")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set up corridor for animation debugging")
    parser.add_argument("--restore", action="store_true", help="Restore original input files from backup")
    parser.add_argument("--num_loops", "-n", type=int, default=5, help="Number of passing loops (default: 5)")
    parser.add_argument("--rate", type=float, default=2.0, help="Mean inter-arrival rate in hours (default: 2.0)")
    parser.add_argument("--frac", type=float, default=0.25, help="Fraction of overlength trains (default: 0.25)")
    parser.add_argument("--sim_len", type=float, default=48, help="Simulation length in hours (default: 48)")
    parser.add_argument("--seed", type=int, default=1, help="Random seed (default: 1)")
    parser.add_argument("--corridor_len", type=float, default=2.0, help="Corridor length in km (default: 2.0)")
    parser.add_argument("--corridor_len_var", type=float, default=0.5, help="Corridor length variation (default: 0.5)")
    parser.add_argument("--loop_len", type=float, default=0.7, help="Passing loop length in km (default: 0.7)")
    parser.add_argument("--pl_max_length", type=float, default=0.7, help="Max train length for PL constraints (default: 0.7)")
    parser.add_argument("--min_length", type=float, default=0.8, help="Min train length for overlength constraints (default: 0.8)")

    args = parser.parse_args()

    if args.restore:
        restore_input()
    else:
        setup(args)
