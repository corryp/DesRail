"""
Generate engineered overlength constraints (nogood_constr.csv) for synthetic corridors.

These prevent head-on deadlocks between overlength trains that cannot fit a passing
loop: a constraint holds an overlength train at its spawn corridor while an opposing
overlength train occupies some position on the network. Each opposing position comes
in two flavours, the train stopped with its head at the near end of the segment it
occupies, and the train having locked through the adjacent loop so its head is beyond
it and its tail still on that segment.

Rewritten 2026-10-15. Two changes.

**Footprint output.** Tuples are emitted in the A5 footprint form, one per train,
as a head-anchored arc sequence "seg:head>seg:head". The pre-A5 segment-level form
is not written any more, so engineered and learned constraints share one language.

**Route-derived lock-through, which fixes 2N+4 inert constraints.** The previous
version took the lock-through position from the neighbouring entry of a west-to-east
list of segments. That list interleaves both arms of every loop, so a list
neighbour is not always a route neighbour, and 2N+4 of the 12N+2 constraints could
never fire (see revision/A5_build/ENGINEERED_INERT.md):

  * 2N asked one train to hold both arms of one loop, which no simple path does;
  * 2 paired a segment with itself in opposite directions, needing two trains on it;
  * 2 had a lock-through footprint overlapping the spawn tuple's segment, likewise.

The lock-through position is now derived from the route: a loop arm has one segment
behind it (the corridor on that side of the loop), while a corridor segment has
**two** (either arm of the adjacent loop), so that case yields two constraints rather
than one. Any constraint whose two footprints share a segment is rejected at
generation time, which is the check the footprint form makes possible.

The constraint count is therefore no longer 12N+2. It is reported at generation.

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

    # --- Geometry: west and east node of every segment ---
    # Route west to east: W_corridor -> {ML_0 | siding_0} -> corridor_0
    #                     -> {ML_1 | siding_1} -> ... -> E_corridor
    # The two arms of a loop are ALTERNATIVES, never consecutive, which is what the
    # previous list-neighbour version got wrong.
    west_node, east_node, segments = {}, {}, []

    def add(seg, w, e):
        west_node[seg], east_node[seg] = w, e
        segments.append(seg)

    add(seg_W_corridor, node_W_junc, node_pl_west(0))
    for i in range(N):
        add(seg_pl_main(i), node_pl_west(i), node_pl_east(i))
        add(seg_pl_siding(i), node_pl_west(i), node_pl_east(i))
        add(seg_corridor_after(i), node_pl_east(i),
            node_pl_west(i + 1) if i < N - 1 else node_E_junc)

    def loop_of_arm(seg):
        """Loop index if seg is one of a loop's two arms, else None."""
        for i in range(N):
            if seg in (seg_pl_main(i), seg_pl_siding(i)):
                return i
        return None

    def corridor_index(seg):
        """i such that seg == seg_corridor_after(i), else None."""
        for i in range(N):
            if seg == seg_corridor_after(i):
                return i
        return None

    def next_west(seg):
        """Segments a westbound train on `seg` would lock through into."""
        if seg == seg_W_corridor:
            return []                      # leaves the network
        i = loop_of_arm(seg)
        if i is not None:                  # a loop arm: one corridor behind it
            return [seg_W_corridor if i == 0 else seg_corridor_after(i - 1)]
        i = corridor_index(seg)            # a corridor: either arm of the loop west of it
        return [seg_pl_main(i), seg_pl_siding(i)]

    def next_east(seg):
        """Segments an eastbound train on `seg` would lock through into."""
        if seg == seg_W_corridor:
            return [seg_pl_main(0), seg_pl_siding(0)]
        i = corridor_index(seg)
        if i is not None:
            if i == N - 1:
                return []                  # E_corridor leaves the network
            return [seg_pl_main(i + 1), seg_pl_siding(i + 1)]
        i = loop_of_arm(seg)               # a loop arm: the corridor ahead of it
        return [seg_corridor_after(i)]

    def wb(seg):  return (seg, west_node[seg])   # arc a westbound train occupies
    def eb(seg):  return (seg, east_node[seg])   # arc an eastbound train occupies

    # Spawn tuples: the held overlength train at its own spawn corridor.
    east_spawn = ([eb(seg_W_corridor)], "East_exit")
    west_spawn = ([wb(seg_E_corridor)], "West_exit")

    # --- Generate constraints ---
    constraints = []   # (id, occupancy_limit, [(footprint, terminal, min_length)])
    cid = 0
    rejected = []

    def emit(spawn, opp_fp, opp_terminal):
        """Add one constraint, unless the two footprints would need to share a segment."""
        nonlocal cid
        spawn_fp, spawn_terminal = spawn
        shared = {s for s, _ in spawn_fp} & {s for s, _ in opp_fp}
        if shared:
            rejected.append((spawn_fp, opp_fp, sorted(shared)))
            return
        constraints.append((f"ol_{cid}", 1, [
            (spawn_fp, spawn_terminal, min_length),
            (opp_fp, opp_terminal, min_length),
        ]))
        cid += 1

    for seg in segments:
        # East spawn held against a westbound opposing train
        emit(east_spawn, [wb(seg)], "West_exit")
        for nxt in next_west(seg):
            emit(east_spawn, [wb(nxt), wb(seg)], "West_exit")
        # West spawn held against an eastbound opposing train
        emit(west_spawn, [eb(seg)], "East_exit")
        for nxt in next_east(seg):
            emit(west_spawn, [eb(nxt), eb(seg)], "East_exit")

    # --- Write CSV, A5 footprint format ---
    def encode(fp):
        return ">".join(f"{seg}:{head}" for seg, head in fp)

    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["constraint_id", "footprint/occupancy_limit", "terminal", "min_length"])
        for c_id, occ_limit, tuples in constraints:
            w.writerow([c_id, occ_limit, "", ""])
            for fp, target, ml in tuples:
                w.writerow([c_id, encode(fp), target, ml])

    print(f"Generated {cid} overlength spawn-hold constraints for N={N}:")
    print(f"  segments covered: {len(segments)} (corridors + both arms of every loop)")
    print(f"  rejected at generation (footprints would share a segment): {len(rejected)}")
    print(f"  output: {output_path}")
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
