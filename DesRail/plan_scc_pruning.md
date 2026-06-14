# SCC Pruning — Remove Redundant Nodes from Deadlock SCCs

## Problem

When generating constraints from deadlock SCCs, some trains are included that have no causal role in the circular wait. This inflates the occupancy limit and adds unnecessary arcs, producing weaker constraints that fail to prevent the core deadlock.

## Evidence (iteration 2 of warm-start at dl_1355)

Conflict graph at t=33 has a 7-train SCC: T120, T125, T128, T131, T126, T123, T122.

**T131 (160-120 → exit_to_Brisbane)** is in the SCC but shouldn't be:
- Its only outgoing SCC edges are **physical blocks** to T125 (@ 159-119) and T128 (@ 158-119)
- Both T125 and T128 are already reachable from the rest of the SCC without T131
- T131 is pulled in solely by an incoming constraint edge: T120 → T131 via **dl_124** @ 35-29
- dl_124 (limit=3, 5 arcs) includes arc 160→121 with front_signal 160→120, which matches T131's position
- T131 is physically queued behind two other trains — it has no agency in the deadlock

**Effect**: Including T131 inflates the generated constraint from limit=5 to limit=6 and adds T131's arcs. The constraint is too loose to prevent the actual 6-train circular wait. This pattern likely repeats across plateau iterations, generating weak constraints that don't resolve the core deadlock — partially explaining long plateaus (e.g., 668 iterations at t=1030).

## Proposed Fix Direction

After finding deadlock SCCs via Tarjan's algorithm, prune nodes that are redundant to the cycle structure. A node is redundant if:
- Its removal does not break the strong connectivity of the remaining SCC
- Equivalently: it is not on any minimal cycle within the SCC

Possible approaches (need careful analysis before implementing):
1. **Physical-only tail pruning**: If a node's only outgoing edges within the SCC are physical blocks (Case a) to nodes already reachable through other SCC-internal paths, prune it. Constraint back-edges pull these nodes in but they contribute only redundant physical forward-edges.
2. **Essential node detection**: Iteratively remove each node and check if the remaining subgraph is still strongly connected. Keep only essential nodes. O(V × (V+E)) but SCCs are typically small (< 20 nodes).
3. **Minimal cycle extraction**: Find the shortest/smallest cycle within the SCC and generate the constraint from that cycle only. More aggressive — may miss legitimate multi-train deadlocks.

## Key Insight — Redundant Constraint Edge Selection (preferred approach)

The original insight was about node pruning, but a sharper formulation emerged: **the problem is redundant constraint edges, not redundant nodes**.

A train blocked by multiple constraints only needs **one** of those constraints to explain why it's stuck. The other constraints' edges are redundant — removing them doesn't change the blocking state of any train. But these redundant edges can pull bystander nodes into the SCC.

### Worked example: T120 in iteration 2

T120 has 4 outgoing SCC edges from two constraints:
```
T120 → T125  via dl_90     (keep)
T120 → T128  via dl_90     (keep)
T120 → T131  via dl_124    (redundant)
T120 → T126  via dl_124    (redundant)
```

**Option A — drop dl_124, keep dl_90**: T131 loses its only incoming SCC edge and drops out. SCC shrinks 7 → 6.

**Option B — drop dl_90, keep dl_124**: T125 and T128 still have incoming edges from T131 (physical) and T126 (dl_90 via T126's own outgoing edges). All 7 nodes remain strongly connected. SCC stays at 7.

The choice is **not arbitrary** — which constraint you keep determines whether pruning helps. This means the algorithm must be smart about selection.

### Algorithm sketch

For each node in the SCC with outgoing edges from multiple blocking sources (multiple constraints, or a mix of physical and constraint edges):
1. Enumerate the distinct constraints contributing outgoing SCC-internal edges
2. For each constraint, check the validity condition: would every affected node retain at least one SCC-internal outbound edge after removing this constraint's edges?
3. Among valid candidates, trial-remove each and check which removal shrinks the SCC most
4. Since SCCs are small (< 20 nodes), trying all options is computationally cheap

This can be applied iteratively — after pruning one node's redundant edges and re-computing the SCC, other nodes may become prunable.

### Physical edge pruning — ruled out

Considered extending the same logic to physical blocking edges (if a node has multiple outgoing physical edges, it only needs one to be "blocked"). **This produces invalid cuts.**

Worked example: 3-train deadlock A→B→C→A where A also has A→C (physical). Pruning A→B shrinks the SCC to {A,C} with limit=1. But the original deadlock requires all three trains — without B, A and C may not deadlock (A would have passed C's position if B hadn't stopped it there). The limit=1 constraint fires on 2-train configurations that are not deadlocks — a false positive.

**Root cause of the asymmetry**: constraint edge pruning removes a redundant *explanation* for the same blocking — the remaining SCC is still a complete explanation of the deadlock. Physical edge pruning removes a non-redundant blocking *relationship* — the remaining SCC is an incomplete explanation, missing a real obstruction that was part of why the deadlock formed. The constraint generated from this incomplete picture fires in states where the full deadlock conditions aren't met.

**Principle**: only prune edges where the pruning cannot change the minimum number of trains needed for the deadlock.

### Extension: physical + constraint mixed blocking

When a train is blocked both physically (Case a) AND by constraints (Case b), the physical edge and constraint edges represent different blocking mechanisms. The physical edge cannot be pruned (it's a unique blocking relationship). The constraint edges CAN be pruned if the physical edge alone keeps the node connected in the SCC — this is the same as constraint-only pruning (the train is blocked regardless by the physical obstruction, so the constraint edges are redundant explanations). Must verify the cycle survives without them.

### Why this is better than node-level pruning

The original approaches (1-3 above) operate on nodes — asking "is this node essential?" The constraint edge selection approach operates on edges — asking "is this edge essential given that the source node has other blocking reasons?" This is more targeted because:
- It preserves all trains that are genuinely part of the cycle
- It only removes edges that are provably redundant (the train is blocked regardless)
- The SCC shrinks as a consequence of edge removal, not as a direct node-pruning step

## Files Involved

- `DesRail/deadlock_avoidance.cpp` — `generate_constraint_from_scc()` (add edge pruning step before constraint generation), or `DeadlockAnalyzer::analyze()` (prune conflict graph edges before SCC detection)
- `DesRail/deadlock_avoidance.h` — may need new helper function declarations
- `ConflictGraph` — edge labels already distinguish constraint source (C:dl_xxx), making it straightforward to group edges by constraint per node

## Validity Condition

**Pruning a constraint's edges from the SCC is valid if and only if: every node in the SCC that has outgoing edges from that constraint retains at least one SCC-internal outbound edge (physical or from a different constraint) after the pruned edges are removed.**

Rationale: the remaining SCC-internal edge guarantees the node is still blocked *within the cycle*, not just blocked in general. An edge to a node outside the SCC doesn't contribute to the circular wait and cannot substitute for the pruned constraint edge.

### Counterexample showing why SCC-internal is required

```
A → C (constraint X, SCC-internal)   ← prune candidate
A → E (physical, OUTSIDE SCC)        ← remaining edge, but external
B → A (physical, B's only outgoing edge)
C → B (physical)
```

Cycle: A→C→B→A. Valid 3-train deadlock. If condition only required "any remaining outbound edge," A retains A→E and the condition is met. But pruning X collapses the entire SCC: A drops (no SCC-internal outgoing edges), B drops (B→A was its only edge), C drops. The deadlock was real — even without E, constraint X keeps A stuck in the cycle. E is irrelevant to the deadlock; it's just an additional, independent reason A can't move.

With the SCC-internal requirement, the condition correctly rejects this pruning: A has zero remaining SCC-internal outbound edges after removing constraint X.

## Design Decisions

- **Constraint-level atomicity**: pruning must keep or drop ALL edges from a given constraint for a given node. Cherry-picking individual arcs from different constraints could produce a hybrid constraint that fires when neither original constraint is violated.
- **Physical edges are not prunable**: each physical edge is a non-redundant blocking relationship. Pruning one produces an SCC that does not give a full explanation of the deadlock — the generated constraint is based on an incomplete picture and fires in states where the full deadlock conditions aren't met (false positives). By contrast, pruning a redundant constraint edge preserves a complete explanation — the remaining SCC fully accounts for why every train is stuck. Only constraint edges are candidates for pruning.

## Implementation Status

Implemented in `prune_redundant_constraint_edges()` (`deadlock_avoidance.cpp`). Called from `DeadlockAnalyzer::analyze()` on each root-cause SCC after Tarjan's and derived SCC filtering.

### Resolved questions (from Open Questions below)

- **Before or after SCC detection?** After. Pruning runs within each identified root-cause SCC, not on the full conflict graph.
- **Does pruning order matter?** Yes, but greedy (pick the removal that shrinks the SCC most) works well in practice. SCCs are small enough to try all options per pass.
- **Interaction with derived SCC filtering?** Pruning runs only on root-cause SCCs (after filtering). Clean separation — derived SCCs are discarded before pruning.
- **Combine with essential node detection?** Not yet attempted. Current edge-based pruning alone produces correct results.

### Known bug (fixed): Pruned constraint label stripping

**Symptom**: At iteration 23 (cold-start, high spawn rate), a 4-node SCC was pruned to 2 nodes instead of 3, producing a limit=1 mutex constraint. This caused a regression from t=33 to t=16 at iteration 24.

**Root cause**: After pruning constraint dl_11's edges in pass 1 (4→3 nodes), dl_11's labels survived on multi-label edges (edges with both dl_11 and dl_25 as sources). Pass 2 saw dl_11 as a valid backup source for those edges, allowing it to also prune dl_25. The result was a 2-node SCC that neither single removal would produce.

**Worked example** (iteration 23, 4-node SCC {T121, T123, T126, T129}):
```
T121→T123: {dl_11, dl_25}   ← multi-label edge
T121→T126: {dl_25}
T121→T129: {dl_11}
```
Pass 1 prunes dl_11 (removes T121→T129, edge T121→T123 survives via dl_25). SCC shrinks to {T121, T123, T126}. Pass 2: without fix, T121→T123 still shows {dl_11, dl_25} as sources, so dl_25 appears backed up. Pruning dl_25 removes T121→T126 AND T121→T123 (dl_25 is the only source — but only because dl_11 was already pruned). SCC collapses to 2 nodes.

**Fix**: Track pruned constraint IDs in a `set<string> pruned_constraints`. When building `target_sources`, filter out labels whose constraint ID has been pruned. Pass 2 then sees T121→T123 as having only {dl_25} — a single source that cannot be pruned without disconnecting the node.

**Diagnostic**: `pruning_details` column added to `dlexp_log.csv`. Format: `orig>final[-cid1,cid2]` per SCC. Confirmed fix: iteration 23 now shows `4>3[-dl_11]`, iteration 24 stays at t=33.

## Pruning Floor Fix — Investigation Results

### Setup
Warm-start at dl_116 (117 constraints), reproducing original iteration 106. Floor fix: trial acceptance changed from `largest >= 2` to `largest >= 3`.

### Iteration 0 conflict graph (t=31)

4-node SCC: T111 (26-21, →Gowrie_exit), T115 (27-21, →exit_to_Brisbane), T113 (24-20, →Gowrie_exit), T112 (35-28, →exit_to_Brisbane).

SCC-internal edges:
| Edge | Source(s) |
|------|-----------|
| T111→T115 | physical |
| T115→T111 | physical + dl_111 |
| T115→T113 | dl_111 only |
| T115→T112 | dl_111 only |
| T113→T111 | physical + dl_116 |
| T113→T115 | dl_116 only |
| T112→T115 | physical |

Without floor fix: pruning dl_111's sole-source edges (T115→T113, T115→T112) leaves {T111, T115} with mutual physical block → limit=1 → dl_117(1|3) → regression.

With floor fix: 2-node residual rejected by `>= 3` threshold. Full 4-node SCC generates dl_117(3|6).

### Key finding: 2-node residual is genuine but over-restrictive

The {T111, T115} mutual physical block IS a real deadlock — opposing trains face-to-face on Rangeview PL. However:
- This configuration only arises when the broader 4-train pattern forces them into position
- limit=1 fires when just 1 train occupies those arcs, blocking entry with no opposing traffic
- The correct constraint is limit=3 from the 4-node SCC: "don't let 4 trains pile up here"

**Conclusion**: The floor fix works but doesn't address the root cause. The 2-node residual isn't an artifact — it's a *necessary condition* within a larger deadlock, but the limit=1 constraint doesn't capture that it needs all 4 trains to manifest.

### Root cause: section-level blocking semantics

The floor fix was superseded by a principled fix (section-level pruning validity, fix #10). The deeper insight: a physical edge to one SCC member only means one *specific section* is blocked. At a passing loop signal, a train has multiple candidate sections (main + siding). The pruned constraint (dl_111) was the sole blocker on the siding section. Removing dl_111's edges leaves the physical edge (main section blocked) — satisfying the old validity condition — but the siding opens, so the train isn't actually stuck.

**Asymmetry**: a constraint edge guarantees the section is blocked (the constraint still fires after pruning a different constraint). A physical edge only guarantees one section is blocked — other sections may have been blocked solely by the pruned constraint.

**Fix**: `get_section_id()` extracts section identity from edge labels. Per-node `section_sources` map groups ALL outgoing edges by section. Prune candidate rejected if it's the sole source for any section.

### Results across 3 warm-start iterations (with section-level fix)

| Iter | Constraint | Pruning | Note |
|------|-----------|---------|------|
| 0 | dl_117(3\|6) | none | Section check rejected dl_111 prune |
| 1 | dl_118(3\|7) | 5>4[-dl_112] | Pruning works where valid |
| 2 | dl_119(4\|8) | none | Target-specific variant |

All at t=31 (plateau), generating target-specific Rangeview constraints. No regressions.

### Full 500-iteration cold-start results (with section-level fix)

| Metric | Pre-fix run | Post-fix run |
|--------|-------------|--------------|
| Regressions | 2 catastrophic (t=58→15, t=34→17) | **Zero** |
| Final deadlock_time | t=34 (stagnated) | **t=842** (still advancing) |
| Final trains | ~124 | **3,463** |
| Total constraints | ~200+ | **526** |
| Limit=1 from pruning | Multiple | **Zero** |
| Learning time | — | 352.8s |

Pruning fires correctly throughout the run (e.g., `6>4`, `8>6`, `7>4`, `11>8`, `12>8`, `15>12`), all producing ≥3-node residuals. The section check blocks invalid prunes while allowing valid ones.

### Full t=2000 cold-start results (with section-level fix)

| Metric | Pre-fix t=2000 run | Post-fix t=2000 run |
|--------|--------------------|--------------------|
| Converged? | No (hit 2000-iter cap) | **Yes (iteration 1643)** |
| Catastrophic regressions | 2 (delta -998, delta -58) | **Zero** |
| Worst drop | -998 | **-1** |
| Final deadlock_time | t=34 (stagnated) | **t=2000 (converged)** |
| Total constraints | 2036 | **1675** |
| Final trains | 122 | **8320** |
| Trains/hr | 3.59 | **4.16** |
| Learning time | ~700s | **6410s** (~1h 47m) |

Key observations:
- The t=1030 mega-plateau persists (~680 iterations, 41% of total) — nearly identical to the pre-fix run's 668 iterations. The pruning fix doesn't shrink this plateau but prevents the catastrophic regression that previously destroyed all progress upon escaping it.
- Pruning fired on 124 of 1643 iterations, typically removing 1-3 nodes (e.g., `11>9`, `15>12`, `8>4`). Most aggressive single pruning: `8>4`. Zero limit=1 constraints from pruning.
- Only 2 limit=1 constraints in the entire run: dl_0 and dl_1 at iterations 0-1 (natural 2-node deadlocks, not from pruning).
- Throughput steady at ~4.16-4.19 trains/hr from iteration 100 onward, regardless of constraint count (107 to 1675).

### Plateau breakdown (converged t=2000 run)

| Plateau | Iterations | % of total |
|---------|-----------|------------|
| t=63 | 124 | 7.5% |
| t=445 | ~143 | 8.7% |
| t=1030 | ~680 | **41.4%** |
| t=1040 | 94 | 5.7% |

The t=1030 plateau is the primary remaining bottleneck for convergence speed improvement.

## Open Questions

- Can this be combined with approach 2 (essential node detection) as a second pass — first prune redundant constraint edges, then check for non-essential nodes?
- What drives the t=1030 mega-plateau (~680 iterations, 41% of total)? Is it target-specific constraint proliferation at a single bottleneck, or a structural issue requiring a different approach (e.g., target-agnostic constraints)?
