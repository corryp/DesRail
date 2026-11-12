#!/bin/bash
# Extend the held-out overlength PL-only benchmark to the corridor's 10,000
# held-out seeds (100000 to 109999), so every overlength arm runs on the same
# seeds. One job per cell; each resumes from the 100 seeds already in
# pl_benchmark_heldout.csv. Nearly every PL-only run deadlocks, which ends it
# early, so these jobs are short.
#
#   bash pbs/submit_heldout10k_pl.sh [--dry-run]
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
			submit "dl_h10k_pl_${pol}_N${N}_f${f}" \
				"${ENVS} STALL_POLICY=${pol} python3 runners/run_overlength.py --phase benchmarks --arms pl --scenarios N${N}:f${f} --jobs 1 --policy ${pol}"
		done
	done
done
