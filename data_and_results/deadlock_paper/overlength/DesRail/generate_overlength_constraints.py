"""
Generate engineered overlength constraints (nogood_constr.csv) for synthetic corridors.

These constraints prevent head-on deadlocks between overlength trains that cannot
fit in passing loops:

Spawn-hold constraints (occ_limit=1): Prevent an overlength train from proceeding
from its spawn corridor if an opposing overlength train is anywhere on the network.
Each opposing segment has two front_signal options:
  - Nearest: the approach signal at the end of the segment
  - Lock_thru: the next signal past the adjacent PL (the train has locked through)
Endpoint segments (W_corridor westbound, E_corridor eastbound) have only one option
since there is no further lock_thru signal.

Constraint count: 8*N + 2

Note: Single-segment PL stop-prevention constraints are no longer needed — overlength
trains can now stop at PLs with tail overhang, controlled by max_length on PL group
constraints in passing_loops.csv (see generate_corridor.py --overlength).

Requires the same corridor topology as generate_corridor.py.
"""

import argparse
import csv
import os


def generate_overlength_constraints(num_loops, output_path, min_length=0.8):
    """Generate nogood_constr.csv for a corridor with num_loops passing loops."""
    N = num_loops
    if N < 1:
        raise ValueError("Need at least 1 passing loop for overlength constraints")

    # --- Topology (must match generate_corridor.py) ---
    # Nodes
    node_W_junc = 1
    def node_pl_west(i): return 2 + 2 * i
    def node_pl_east(i): return 3 + 2 * i
    node_E_junc = 2 * N + 2

    # Segments
    seg_W_corridor = 2
    def seg_pl_main(i): return 3 + 3 * i
    def seg_pl_siding(i): return 4 + 3 * i
    def seg_corridor_after(i): return 5 + 3 * i  # corridor after PL_i
    seg_E_corridor = 3 * N + 2  # = seg_corridor_after(N-1)

    # --- Build signal sequence (west to east) ---
    # Each entry: (segment_id, eb_front_signal, wb_front_signal)
    # For eastbound trains, front_signal is the signal at the east end of each segment.
    # For westbound trains, front_signal is the signal at the west end.
    #
    # Ordered segments west to east:
    #   W_corridor, PL_0_main, PL_0_siding, corridor_0, PL_1_main, PL_1_siding, ..., E_corridor
    # Both main and siding are included because overlength trains can stop at either
    # (with tail overhang onto approach).

    ordered_segments = []  # (seg_id, eb_front_signal, wb_front_signal)

    # W_corridor (seg 2, nodes W_junc→PL_0_west)
    ordered_segments.append((
        seg_W_corridor,
        (seg_W_corridor, node_pl_west(0)),   # eastbound: front at PL_0 west
        (seg_W_corridor, node_W_junc),        # westbound: front at W_junc
    ))

    for i in range(N):
        pw = node_pl_west(i)
        pe = node_pl_east(i)

        # PL_i main (seg 3+3i, nodes PL_i_east→PL_i_west [reversed])
        ordered_segments.append((
            seg_pl_main(i),
            (seg_pl_main(i), pe),   # eastbound: front at PL_i east
            (seg_pl_main(i), pw),   # westbound: front at PL_i west
        ))

        # PL_i siding (seg 4+3i, nodes PL_i_west→PL_i_east)
        ordered_segments.append((
            seg_pl_siding(i),
            (seg_pl_siding(i), pe),   # eastbound: front at PL_i east
            (seg_pl_siding(i), pw),   # westbound: front at PL_i west
        ))

        # Corridor after PL_i
        if i < N - 1:
            # Inter-PL corridor (PL_i_east → PL_{i+1}_west)
            ordered_segments.append((
                seg_corridor_after(i),
                (seg_corridor_after(i), node_pl_west(i + 1)),  # eastbound
                (seg_corridor_after(i), node_pl_east(i)),       # westbound
            ))
        else:
            # E_corridor (PL_{N-1}_east → E_junc)
            ordered_segments.append((
                seg_E_corridor,
                (seg_E_corridor, node_E_junc),          # eastbound
                (seg_E_corridor, node_pl_east(N - 1)),   # westbound
            ))

    # Spawn tuples (the overlength train at its spawn corridor)
    east_spawn = (seg_W_corridor, "East_exit", seg_W_corridor, node_pl_west(0))
    west_spawn = (seg_E_corridor, "West_exit", seg_E_corridor, node_pl_east(N - 1))

    # --- Generate constraints ---
    constraints = []  # list of (constraint_id, list_of_tuples)
    # Each tuple: (seg_id, terminal, fs_seg, fs_head, min_length, max_length)
    cid = 0

    # ===== Spawn-hold constraints =====
    num_segs = len(ordered_segments)

    for k, (seg_id, eb_fs, wb_fs) in enumerate(ordered_segments):
        # --- East spawn vs westbound opposing ---
        # Nearest front_signal for westbound on this segment
        wb_nearest = wb_fs
        # Lock_thru: the wb_fs of the previous segment (one step further west)
        wb_lockthru = ordered_segments[k - 1][2] if k > 0 else None

        # Constraint: east spawn + westbound nearest
        tuples = [
            (east_spawn[0], east_spawn[1], east_spawn[2], east_spawn[3], min_length, 0.0),
            (seg_id, "West_exit", wb_nearest[0], wb_nearest[1], min_length, 0.0),
        ]
        constraints.append((f"ol_{cid}", 1, tuples))
        cid += 1

        # Constraint: east spawn + westbound lock_thru (if exists)
        if wb_lockthru is not None:
            tuples = [
                (east_spawn[0], east_spawn[1], east_spawn[2], east_spawn[3], min_length, 0.0),
                (seg_id, "West_exit", wb_lockthru[0], wb_lockthru[1], min_length, 0.0),
            ]
            constraints.append((f"ol_{cid}", 1, tuples))
            cid += 1

        # --- West spawn vs eastbound opposing ---
        eb_nearest = eb_fs
        # Lock_thru: the eb_fs of the next segment (one step further east)
        eb_lockthru = ordered_segments[k + 1][1] if k < num_segs - 1 else None

        # Constraint: west spawn + eastbound nearest
        tuples = [
            (west_spawn[0], west_spawn[1], west_spawn[2], west_spawn[3], min_length, 0.0),
            (seg_id, "East_exit", eb_nearest[0], eb_nearest[1], min_length, 0.0),
        ]
        constraints.append((f"ol_{cid}", 1, tuples))
        cid += 1

        # Constraint: west spawn + eastbound lock_thru (if exists)
        if eb_lockthru is not None:
            tuples = [
                (west_spawn[0], west_spawn[1], west_spawn[2], west_spawn[3], min_length, 0.0),
                (seg_id, "East_exit", eb_lockthru[0], eb_lockthru[1], min_length, 0.0),
            ]
            constraints.append((f"ol_{cid}", 1, tuples))
            cid += 1

    # --- Write CSV ---
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["constraint_id", "segment_id/occupancy_limit", "terminal",
                     "front_signal_segment_id", "front_signal_head_node_id", "min_length", "max_length"])
        for c_id, occ_limit, tuples in constraints:
            # Header row with occ_limit
            w.writerow([c_id, occ_limit, "", "", "", "", ""])
            # Tuple rows
            for seg, target, fs_seg, fs_head, ml, xl in tuples:
                w.writerow([c_id, seg, target, fs_seg, fs_head, ml, xl])

    print(f"Generated {cid} overlength constraints for N={N} corridor:")
    print(f"  Spawn-hold (opposing overlength): {cid}")
    print(f"  Segments covered: {len(ordered_segments)} (corridors + mainlines + sidings)")
    print(f"  Output: {output_path}")
    return cid


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate engineered overlength constraints for synthetic corridors")
    parser.add_argument("--num_loops", "-n", type=int, required=True,
                        help="Number of passing loops (must match corridor topology)")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Output file path (default: input/nogood_constr.csv relative to CWD)")
    parser.add_argument("--min_length", type=float, default=0.8,
                        help="Minimum train length to trigger constraints (default: 0.8)")

    args = parser.parse_args()

    if args.output is None:
        args.output = os.path.join("input", "nogood_constr.csv")

    generate_overlength_constraints(args.num_loops, args.output, args.min_length)
