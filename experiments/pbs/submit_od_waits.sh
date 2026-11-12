#!/bin/bash
# Per-train waits by origin-destination pair on the Toowoomba hybrid grid
# (review comment 1.m2): the 12 cells on held-out seeds 100000 to 100099, discard
# policy, with OD_LOG=1. Writes benchmark_heldout_od.csv and od_waits_heldout_od.csv
# per cell, beside the held-out benchmark files, which it does not touch.
#
#   bash pbs/submit_od_waits.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=dl_od_toowoomba_discard
CMD="BENCH_SEED_START=100000 BENCH_SEED_COUNT=100 OD_LOG=1 STALL_POLICY=discard python3 runners/run_toowoomba_hybrid_benchmark.py --jobs 1 --policy discard"
mkdir -p pbs/done pbs/logs
rm -f "pbs/done/${NAME}.done"
if [ "${1:-}" = "--dry-run" ]; then echo "would submit ${NAME}: ${CMD}"; exit 0; fi
/opt/pbs/bin/qsub -N "${NAME}" -v RUNCMD="${CMD}" pbs/cell.pbs | sed "s/^/submitted ${NAME} /"
