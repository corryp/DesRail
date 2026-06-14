# Constraint Check Optimisation — Incremental Occupancy Counters

## Context

With 2000+ generated constraints, `is_violated()` is called O(blocked_trains × constraints_per_arc) times every time a segment frees and `grant_access()` re-evaluates queued requests. Each call iterates over all constraint arcs checking segment ownership. Future simulation-optimisation and RL work will run thousands of simulation replications where even hundredths of a second per iteration compound.

## High-Level Tasks

### 1. Profile current constraint overhead
- Instrument `is_violated()` call count and cumulative time per simulation run
- Compare runs with 0, ~400, and ~2000 constraints to quantify the actual cost
- Identify whether the bottleneck is call frequency or per-call cost

### 2. Design incremental occupancy tracking
- Each `SignalConstraint` maintains a live set/count of trains currently in its footprint
- Determine how to handle the target-matching and front_signal logic — can trains be filtered at update time, or does violation checking still need per-train inspection?
- Identify the segment state-change points where updates must happen (grant, release, train destruction)

### 3. Implement and integrate
- Hook into segment ownership changes to update constraint occupancy sets
- Replace iterative counting in `is_violated()` with set-size comparison
- Ensure correctness with the requesting-train logic (the train asking for access isn't occupying yet but needs to be counted)

### 4. Constraint dominance pruning (independent)
- After all constraints are generated, identify and remove dominated constraints (A dominates B if A's arcs ⊆ B's arcs and A's limit ≤ B's limit)
- One-time cleanup, no runtime overhead

### 5. Constraint merging exploration (independent)
- Investigate whether constraints sharing the same arc set but different target filters can be merged into a single multi-target constraint
- Assess how common this pattern is in practice from generated constraint data

### 6. Verify
- Compare simulation outcomes (deadlock detection times, train counts) before and after to confirm no behavioural change
- Benchmark wall-clock time improvement across constraint counts
