#!/bin/bash
# Re-benchmark the overlength and Toowoomba hybrid arms on held-out seeds.
#
# The 2026-10-22 campaign benchmarked both on seeds 1 to 100, the seeds training
# starts from, so those benchmarks are in-sample (review comment 1.8). This runs
# the same benchmarks, on the same trained sets, on seeds 100000 to 100099, the
# start of the range the corridor held-out evaluation draws from and training
# cannot reach. Results go to *_heldout.csv beside the originals.
#
# Overlength: phases 1, 3 and 4 (PL only, hybrid, engineered 6N), all 15 cells.
# Toowoomba: the 4x3 hybrid grid. One job per network and stall policy, since
# every cell is minutes of CPU.
#
#   bash pbs/submit_heldout_benchmarks.sh
#   bash pbs/submit_heldout_benchmarks.sh --dry-run
set -euo pipefail
cd "$(dirname "$0")/.."
POLICIES=${POLICIES:-"discard bank"}
DRY=${1:-}
QSUB=/opt/pbs/bin/qsub
mkdir -p pbs/done pbs/logs

for pol in $POLICIES; do
	for job in overlength toowoomba; do
		NAME="dl_hob_${job}_${pol}"
		if [ "$job" = overlength ]; then
			CMD="BENCH_SEED_START=100000 STALL_POLICY=${pol} python3 runners/run_overlength.py --phase benchmarks --jobs 1"
		else
			CMD="BENCH_SEED_START=100000 STALL_POLICY=${pol} python3 runners/run_toowoomba_hybrid_benchmark.py --jobs 1 --policy ${pol}"
		fi
		rm -f "pbs/done/${NAME}.done"
		if [ "$DRY" = "--dry-run" ]; then echo "would submit ${NAME}: ${CMD}"; continue; fi
		$QSUB -N "${NAME}" -v RUNCMD="${CMD}" pbs/cell.pbs | sed "s/^/submitted ${NAME} /"
	done
done
