"""
Verify segment-based constraint simplification on generated_constraints.csv.

Checks:
1. Invariant: occupancy_limit == unique_segments - 1 for every constraint
2. Uniqueness: each segment has at most one (front_signal, target) per constraint
3. Summary stats: total arcs, unique segment tuples, dedup ratio

Usage: python verify_segment_constraints.py path/to/generated_constraints.csv
"""

import csv
import sys
from collections import defaultdict


def parse_constraints(path):
    """Parse generated_constraints.csv into a list of constraint dicts."""
    constraints = []
    current = None

    with open(path, newline='') as f:
        reader = csv.reader(f)
        header = next(reader)  # skip header

        for row in reader:
            if len(row) < 6:
                continue
            cid = row[0].strip()
            col1 = row[1].strip()
            head_node = row[2].strip()

            if not head_node:
                # Header row: constraint_id, occupancy_limit, empty...
                current = {
                    'id': cid,
                    'occupancy_limit': int(col1),
                    'arcs': []
                }
                constraints.append(current)
            elif current and current['id'] == cid:
                # Arc row
                current['arcs'].append({
                    'segment_id': int(col1),
                    'head_node_id': int(head_node),
                    'terminal': row[3].strip(),
                    'front_signal_seg': int(row[4]) if row[4].strip() else -1,
                    'front_signal_head': int(row[5]) if row[5].strip() else -1,
                })

    return constraints


def verify(constraints):
    """Run all verification checks. Returns (pass_count, fail_count, messages)."""
    total_arcs = 0
    total_seg_tuples = 0
    invariant_pass = 0
    invariant_fail = 0
    uniqueness_pass = 0
    uniqueness_fail = 0
    messages = []
    gap_distribution = defaultdict(int)  # (unique_segs - 1 - limit) -> count
    constraint_details = []  # per-constraint analysis data

    for c in constraints:
        cid = c['id']
        limit = c['occupancy_limit']
        arcs = c['arcs']
        total_arcs += len(arcs)
        scc_size = limit + 1  # inferred from limit = max(1, scc_size - 1)

        # Build segment-level tuples: (segment_id, front_signal_key, terminal)
        # where front_signal_key = (front_signal_seg, front_signal_head)
        seg_tuples = set()
        for a in arcs:
            seg_tuple = (
                a['segment_id'],
                (a['front_signal_seg'], a['front_signal_head']),
                a['terminal']
            )
            seg_tuples.add(seg_tuple)

        unique_segments = len(set(a['segment_id'] for a in arcs))
        n_seg_tuples = len(seg_tuples)
        total_seg_tuples += n_seg_tuples

        gap = unique_segments - 1 - limit
        gap_distribution[gap] += 1

        # Count unique front_signal arcs per constraint
        unique_front_signals = len(set(
            (a['front_signal_seg'], a['front_signal_head']) for a in arcs
        ))

        constraint_details.append({
            'id': cid, 'limit': limit, 'scc_size': scc_size,
            'arcs': len(arcs), 'unique_segments': unique_segments,
            'seg_tuples': n_seg_tuples, 'gap': gap,
            'unique_front_signals': unique_front_signals,
        })

        # Check 3: unique_front_signals == scc_size (limit + 1)
        # This is the real invariant: each train in the SCC contributes
        # exactly one front_signal, so the number of distinct front_signals
        # equals the number of trains = scc_size = limit + 1
        if unique_front_signals != scc_size:
            messages.append(
                f"  FAIL front_signals: {cid} unique_front_signals={unique_front_signals} "
                f"scc_size={scc_size} (expected equal)"
            )

        # Check 1: occupancy_limit == unique_segments - 1
        if limit == unique_segments - 1:
            invariant_pass += 1
        else:
            invariant_fail += 1

        # Check 2: each segment has at most one (front_signal, target) per constraint
        seg_qualifications = defaultdict(set)
        for a in arcs:
            qual = ((a['front_signal_seg'], a['front_signal_head']), a['terminal'])
            seg_qualifications[a['segment_id']].add(qual)

        multi_qual = {seg: quals for seg, quals in seg_qualifications.items() if len(quals) > 1}
        if not multi_qual:
            uniqueness_pass += 1
        else:
            uniqueness_fail += 1
            for seg, quals in multi_qual.items():
                messages.append(
                    f"  FAIL uniqueness: {cid} segment {seg} has {len(quals)} qualifications: {quals}"
                )

    return {
        'n_constraints': len(constraints),
        'total_arcs': total_arcs,
        'total_seg_tuples': total_seg_tuples,
        'invariant_pass': invariant_pass,
        'invariant_fail': invariant_fail,
        'uniqueness_pass': uniqueness_pass,
        'uniqueness_fail': uniqueness_fail,
        'messages': messages,
        'gap_distribution': dict(gap_distribution),
        'constraint_details': constraint_details,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python verify_segment_constraints.py [--detail CID] path/to/generated_constraints.csv")
        sys.exit(1)

    detail_cid = None
    path = sys.argv[-1]
    if '--detail' in sys.argv:
        idx = sys.argv.index('--detail')
        detail_cid = sys.argv[idx + 1]

    constraints = parse_constraints(path)

    if not constraints:
        print(f"No constraints found in {path}")
        sys.exit(1)

    results = verify(constraints)

    print(f"File: {path}")
    print(f"Constraints: {results['n_constraints']}")
    print(f"Total arcs: {results['total_arcs']}")
    print(f"Total segment tuples (deduplicated): {results['total_seg_tuples']}")
    dedup_ratio = results['total_arcs'] / results['total_seg_tuples'] if results['total_seg_tuples'] > 0 else 0
    print(f"Dedup ratio: {dedup_ratio:.2f}x")
    print()

    print(f"Invariant (limit == unique_segs - 1): {results['invariant_pass']} pass, {results['invariant_fail']} fail")
    print(f"Uniqueness (one qual per segment):    {results['uniqueness_pass']} pass, {results['uniqueness_fail']} fail")

    # Count front_signal invariant failures
    details = results['constraint_details']
    fs_fail = sum(1 for d in details if d['unique_front_signals'] != d['scc_size'])
    fs_pass = len(details) - fs_fail
    print(f"Front signals (== scc_size):          {fs_pass} pass, {fs_fail} fail")

    # Gap distribution (unique_segments - 1 - limit)
    gap_dist = results['gap_distribution']
    print()
    print("Gap distribution (unique_segs - 1 - limit):")
    for gap in sorted(gap_dist.keys()):
        label = "exact" if gap == 0 else f"+{gap} extra segs"
        print(f"  gap={gap} ({label}): {gap_dist[gap]} constraints")

    # Segment surplus analysis
    details = results['constraint_details']
    if details:
        print()
        print("Segment surplus analysis (gap > 0 constraints):")
        surplus = [d for d in details if d['gap'] > 0]
        if surplus:
            # Group by gap value, show scc_size ranges
            from collections import Counter
            for gap_val in sorted(set(d['gap'] for d in surplus)):
                group = [d for d in surplus if d['gap'] == gap_val]
                scc_sizes = [d['scc_size'] for d in group]
                seg_counts = [d['unique_segments'] for d in group]
                print(f"  gap={gap_val}: n={len(group)}, "
                      f"scc_size={min(scc_sizes)}-{max(scc_sizes)}, "
                      f"unique_segs={min(seg_counts)}-{max(seg_counts)}")

    # Show detail for a specific constraint
    if detail_cid:
        print()
        c = next((c for c in constraints if c['id'] == detail_cid), None)
        if c:
            print(f"Detail for {detail_cid} (limit={c['occupancy_limit']}):")
            # Group arcs by segment
            by_seg = defaultdict(list)
            for a in c['arcs']:
                by_seg[a['segment_id']].append(a)
            for seg_id in sorted(by_seg.keys()):
                arcs = by_seg[seg_id]
                print(f"  segment {seg_id}:")
                for a in arcs:
                    print(f"    head={a['head_node_id']} target={a['terminal']} "
                          f"front_signal=({a['front_signal_seg']},{a['front_signal_head']})")
        else:
            print(f"Constraint {detail_cid} not found")

    if results['messages']:
        print()
        n_show = min(20, len(results['messages']))
        print(f"First {n_show} failures (of {len(results['messages'])}):")
        for msg in results['messages'][:n_show]:
            print(msg)

    if results['invariant_fail'] == 0 and results['uniqueness_fail'] == 0:
        print()
        print("ALL CHECKS PASSED")
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
