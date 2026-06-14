# Memory Allocation Strategy for Multi-Threaded Simulation

## Context

Future RL and simulation-optimisation work will run many simulations in parallel (multi-threading). The global heap allocator becomes a bottleneck when threads contend on `malloc`/`free` locks. This simulation is allocation-heavy: every iteration rebuilds Sim, SignalsManager, all Train objects, LogFile/LogField instances, coroutine frames, speed profile vectors, ConflictGraph nodes, etc.

## Approach: Thread-Local Arena Allocators (Level 1)

Each thread gets a pre-allocated memory pool (arena). All allocations within a simulation run draw from the thread's arena. At end of run, reset the arena pointer — instant bulk deallocation. No global lock contention because each thread owns its pool.

### Why not object pooling?
Object pooling (Level 2) requires per-type pool management, return-to-pool logic, and careful reinitialisation. It's more code, more bugs, and only helps for specific types. Arenas help everything — including STL containers, vectors, small temporaries — with zero changes to call sites.

### Why not fully static layout?
Pre-allocating fixed arrays and using indices instead of pointers (Level 3) eliminates all heap allocation but requires rewriting how objects reference each other throughout the codebase. Only justified if arena approach proves insufficient, which is unlikely at this simulation scale.

## Implementation Outline

### 1. Arena allocator (~100 lines)
- Fixed-size memory block per thread (e.g., 64MB)
- Bump-pointer allocation (fast: just increment a pointer)
- Reset to zero between iterations (no per-object destructor calls needed for POD-like data)
- Fall back to system malloc if arena exhausted (safety net)

### 2. Hook into simulation types
- Override `operator new`/`operator delete` for key classes (Train, SimObject, ConflictNode, LogFile, LogField) to route through thread-local arena
- Alternatively, use a global thread_local arena and override the default allocator for the simulation context

### 3. Coroutine frame allocation
- C++20 allows `promise_type::operator new` customisation
- Route SimCoroutine frames through the arena
- Needs care: coroutine frames must outlive their last suspension point

### 4. STL container allocations
- Vectors, maps, sets used throughout (train->arcs, adjacency lists, etc.)
- Can provide a custom allocator template parameter, or use arena-aware global allocator during simulation scope
- Most invasive part — evaluate whether it's worth the template noise or if arena-scoped global override is cleaner

### 5. Verify
- Run single-threaded with arena allocator, compare outputs to baseline
- Profile allocation counts and time with/without arena
- Then enable multi-threading and measure scaling

## Notes
- The existing `reset()` pattern (RailSimMaster::reset, Sim::reset, RailNetwork::reset) already conceptually aligns with arena reset — destroy everything, start fresh
- `clear_master_logs()` and Train/LogFile destruction would need review to ensure no dangling references across arena resets
- Start with a generous arena size and log high-water marks to right-size later
