#!/bin/bash
# Extend the held-out overlength and Toowoomba hybrid benchmarks to the
# corridor's 10,000 held-out seeds (100000 to 109999), so every network's
# deadlock-free claim rests on the same number of seeds. Only the arms that
# claim zero deadlocks are extended: overlength hybrid and engineered, and the
# Toowoomba hybrid grid. PL only deadlocks on nearly every seed and stays at 100.
#
# One job per cell; each resumes from the seeds already in *_heldout.csv. After
# all Toowoomba jobs finish, run the full grid once to write summary_heldout.csv:
#   BENCH_SEED_START=100000 BENCH_SEED_COUNT=10000 python3 \
#     runners/run_toowoomba_hybrid_benchmark.py --jobs 1 --policy <pol>
#
#   bash pbs/submit_heldout10k.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
POLICIES=${POLICIES:-"discard bank"}
DRY=${1:-}
QSUB=/opt/pbs/bin/qsub
ENVS="BENCH_SEED_START=100000 BENCH_SEED_COUNT=10000"
mkdir -p pbs/done pbs/logs

submit() {
	rm -f "pbs/done/$1.done"
	if [ "$DRY" = "--dry-run" ]; then echo "would submit $1: $2"; return; fi
	$QSUB -N "$1" -v RUNCMD="$2" pbs/cell.pbs | sed "s/^/submitted $1 /"
}

for pol in $POLICIES; do
	for N in 02 05 10 15 20; do
		for f in 0.10 0.25 0.50; do
			submit "dl_h10k_ol_${pol}_N${N}_f${f}" \
				"${ENVS} STALL_POLICY=${pol} python3 runners/run_overlength.py --phase benchmarks --arms hybrid engineered --scenarios N${N}:f${f} --jobs 1 --policy ${pol}"
		done
	done
	for b in 0.25 0.75 1.25 1.75; do
		for c in 0.25 0.50 0.75; do
			submit "dl_h10k_tw_${pol}_b${b}_c${c}" \
				"${ENVS} STALL_POLICY=${pol} python3 runners/run_toowoomba_hybrid_benchmark.py --jobs 1 --policy ${pol} --cells b${b}_c${c}"
		done
	done
done
