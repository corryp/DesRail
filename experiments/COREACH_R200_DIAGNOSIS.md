# Coreachability r2.00 "divergences": diagnosis

**TL;DR:** They are **not** over-restriction and **not** a theory/implementation bug. The learned constraints are sound and firing correctly. The "divergent" seeds are seeds we **mislabelled as safe** because their deadlock forms in the last ~2h before the sim horizon, too late for the deadlock detector to declare it. The theorem holds; the fix is in the eval harness.

## The claim under test

Coreachability-preservation: a self-contained learned set only disables transitions into doomed states, so on an already deadlock-free (coreachable) run it must be a byte-identical no-op. The coreach check found ~1-3% of "safe" seeds at r2.00 (and 1 at N02/r1.00) where the union set changed the log. If those seeds were truly safe, that would break the theorem.

## What is actually happening

The safe-seed oracle calls a seed safe iff `deadlock_found == 0` at `sim_len = 192h`. But the detector needs `DEADLOCK_TIMEOUT = 2h` of no-progress to *declare* a deadlock. A gridlock that forms after ~t=190 never accumulates its 2h window before the run ends, so it is silently missed and the seed looks safe. The learned constraint then correctly refuses the move that completes that gridlock, and the old check scored the (correct) difference as a "divergence".

## Proof (N02, then N05)

Bisected N02/r1.00 seed 100199: the sole binder is **`dl_1`**, an occupancy cut over the four PL_0 arcs `(2-2) (3-2) (4-3) (5-3)`. Fire trace at t=190.1:

```
CFIRE t=190.1 dl_1 T361(REQ, seg5 W)
  [s2: T363 East  reserved seg2]
  [s3: T360 West  on PL_0 main]
  [s4: T362 East  on PL_0 siding]
  [s5: REQ  T361]
```

All four arcs held by target-matching trains, **all four stopped (v=0)** — a would-be gridlock snapshot. The eval counts correctly (not a false positive). In the unprotected run those four trains freeze at t≈190.2 and never move again, yet `deadlock_found=0` because the sim ends at 192.

Extend the horizon and the same gridlock is detected:

| sim_len | unprotected result |
|--------:|--------------------|
| 192 | deadlock_found=0 (missed) |
| 195 | deadlock_found=1 @ t=193.0 |
| 200 | deadlock_found=1 @ t=193.0 |
| 210 | deadlock_found=1 @ t=193.0 |

`deadlock_time` is invariant to how far we extend → it is the *same* early gridlock detected late, not new end-window traffic.

**All 9 N02 divergent seeds** (r1.00: 100199; r2.00: 100070, 100322, 101315, 101577, 101623, 101857, 102422, 102964) are deadlock-free@192 but deadlock@196 at t=193-194. **9/9 late-deadlock.**

**N05 generality:** the 3 N05/r2.00 divergent seeds (100332, 100490, 101425) — same: safe@192, deadlock@198 at t=193-194.

**Control (rules out "extending just deadlocks everything"):** 12 original *no-op* (truly-safe) N05/r2.00 seeds re-run to 198h: 11 stayed safe; the one exception (100043) deadlocked at **t=198.0 — exactly the new horizon**, i.e. a genuinely new deadlock from the extra (192,198] traffic, clearly distinct from the divergent seeds' t=193-194. Divergent seeds are doomed *within* [0,192]; truly-safe seeds are not.

## Why it is rate-dependent

Higher spawn rate → more trains active near the horizon → higher chance a deadlock forms in the final 2h detection-blind window → more mislabelled-safe seeds. Exactly the observed r2.00 ≫ r1.00 pattern.

## Conclusion and fix

- The coreachability-preservation theorem **holds**. The C++ constraint generation and the tree evaluator are **sound**; `dl_1` and its peers fire correctly.
- The defect is the **safe-seed oracle** in the eval harness: it must certify safety past the detection-timeout tail before declaring a seed safe.
- Recommended fix: run the unprotected classification to `sim_len`, then continue the event loop with **spawning disabled** until quiescence (or +`DEADLOCK_TIMEOUT`). That isolates the `[0, sim_len]` trains and detects any late gridlock without injecting new traffic. Then compare protected vs unprotected over `[0, sim_len]`. Truly-safe seeds → 100% no-op.
- **Nothing to change in the paper's safety result** (protected runs: 0 deadlocks). The residual-risk / held-out-safe framing should just note that safe-seed classification runs past the detection tail (otherwise a tiny fraction of near-horizon doomed seeds get miscounted as safe).

## Repro

- `diag_coreach.py N rate seed...` — reproduce + bisect the union to the binding constraint, with CFIRE trace.
- `diag_coreach_fixed.py N rate` — corrected oracle (extended-horizon gate; note the gate adds traffic, prefer the spawn-off variant for a production fix).
- Work dir: `work/diag_coreach/`. Uses freshly-trained N02/N05 corridor sets under `work/corridor/`.
