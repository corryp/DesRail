#!/bin/bash
# Held-out residual of the Toowoomba pure sweep's learned sets: each of the seven
# rates (lambda_B 0.20 to 0.80, lambda_C 0.50) benchmarked with its own set on
# seeds 100000 to 109999, PL constraints off, discard policy. One job per rate;
# each resumes from the seeds already done. Writes work/ and results/
# toowoomba_pure_benchmark/. After all seven finish, run the full sweep once to
# write summary_heldout.csv:
#   PURE=1 BENCH_SEED_START=100000 BENCH_SEED_COUNT=10000 STALL_POLICY=discard \
#     python3 runners/run_toowoomba_hybrid_benchmark.py --jobs 1 --policy discard
#
#   bash pbs/submit_pure_heldout.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
DRY=${1:-}
mkdir -p pbs/done pbs/logs
for b in 0.20 0.30 0.40 0.50 0.60 0.70 0.80; do
	NAME="dl_pure_ho_b${b}"
	CMD="PURE=1 BENCH_SEED_START=100000 BENCH_SEED_COUNT=10000 STALL_POLICY=discard python3 runners/run_toowoomba_hybrid_benchmark.py --jobs 1 --policy discard --cells b${b}_c0.50"
	rm -f "pbs/done/${NAME}.done"
	if [ "$DRY" = "--dry-run" ]; then echo "would submit ${NAME}: ${CMD}"; continue; fi
	/opt/pbs/bin/qsub -N "${NAME}" -v RUNCMD="${CMD}" pbs/cell.pbs | sed "s/^/submitted ${NAME} /"
done
