#!/bin/bash
# Generate this bundle's scenario configs. Run once, from the bundle directory,
# before the harness submits anything.
#
#   cd experiments/2026-07-05_bugfix_rerun && bash pbs/make_configs.sh
#
# Why this file exists. Every CSV in the repository is gitignored (the generator
# is the source of truth, per the root CLAUDE.md), so `configs/` and
# `configs_overlength/` do not travel with a git clone and have to be built on
# whatever machine runs the campaign. The invocations used to live inside
# `generate_base_configs()` in run_experiments.py and a near-copy in
# run_overlength_exp.py, which is exactly the situation the repository's
# reproducibility rule is about: the script was committed but the invocation was
# not. These are the actual invocations, in order, with the flags.
#
# The generator is seeded, so this reproduces the same networks anywhere. The
# drift check: regenerating over an existing configs/ must leave it byte
# identical. It did on 2026-10-19, apart from three files whose shipped copies
# predated rows the generator now writes (`pl_constraint_mode,1`, the unused
# `freight_long` template, and three dlexp_options rows restating code defaults).
#
# NB `--pl_max_length` is deliberately left at its default for the overlength
# set. The flag does two unrelated things: it sets the passing-loop max_length
# column, which the engine now reads and ignores (rail_sim_tools.cpp:1149), and
# it gates the separate long-train spawn and exit segments, which the overlength
# scenarios need. Passing 0 to disable the former would silently remove the
# latter. See decision queue, rerun note 2.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$(dirname "$HERE")"

# Resolve the generator the way the runners resolve the repo: DESRAIL_REPO if
# set (pbs/cell.pbs exports it), else the bundle's usual place inside the repo,
# else a DesRail directory beside the bundle, which is how a deployed campaign
# is laid out on the HPC where the bundle is copied out of the tree.
GEN=""
for cand in "${DESRAIL_REPO:-}/DesRail" "$BUNDLE/../../DesRail" "$BUNDLE/../DesRail"; do
	if [ -f "$cand/generate_corridor.py" ]; then
		GEN="$(cd "$cand" && pwd)/generate_corridor.py"
		break
	fi
done
[ -n "$GEN" ] || {
	echo "generate_corridor.py not found. Looked beside DESRAIL_REPO, two levels" >&2
	echo "up from the bundle, and next to it. Set DESRAIL_REPO to the repo root." >&2
	exit 1
}
echo "generator: $GEN"

CORRIDOR_LEN=2.0
CORRIDOR_LEN_VAR=0.5
LOOP_LEN=0.7
HORIZON=192        # 168 h collection + 24 h warmup
WARMUP=24

for N in 2 5 10 15 20; do
	NN=$(printf '%02d' "$N")
	echo "=== corridor N$NN"
	python3 "$GEN" \
		--num_loops "$N" \
		--output_dir "$BUNDLE/configs/N$NN" \
		--corridor_len "$CORRIDOR_LEN" \
		--corridor_len_var "$CORRIDOR_LEN_VAR" \
		--loop_len "$LOOP_LEN" \
		--spawn_rate 1.0 \
		--horizon "$HORIZON" \
		--warmup "$WARMUP" \
		--seed 1
	echo "=== overlength N$NN"
	python3 "$GEN" \
		--num_loops "$N" \
		--output_dir "$BUNDLE/configs_overlength/N$NN" \
		--corridor_len "$CORRIDOR_LEN" \
		--corridor_len_var "$CORRIDOR_LEN_VAR" \
		--loop_len "$LOOP_LEN" \
		--spawn_rate 1.0 \
		--horizon "$HORIZON" \
		--warmup "$WARMUP" \
		--seed 1 \
		--overlength
done

echo
echo "Spawn rates and overlength fractions are set per cell by the runners, not"
echo "here: the 1.0 above is the generator's placeholder."
echo "Toowoomba uses the frozen config in tmba_input/, which is not generated."
