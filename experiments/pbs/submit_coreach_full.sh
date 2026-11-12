#!/bin/bash
# Re-certify the coreachability no-op on the FULL pooled union, which keeps the
# non-converged scenarios' sets. Only N=20 needs it: under the discarding variant
# every other corridor converged at every rate, so its full union is identical to
# its converged-only union and the existing result still stands.
#
# The seeds are the drained-safe ones of results/heldout_drained/<policy>/safe_seeds/,
# so rates with none are skipped rather than submitted empty.
#
#   bash pbs/submit_coreach_full.sh
#   bash pbs/submit_coreach_full.sh --dry-run
set -euo pipefail
cd "$(dirname "$0")/.."
POLICY=${POLICY:-discard}
NVAL=${NVAL:-20}
RATES=${RATES:-"1.00 1.25 1.50 1.75 2.00 2.25 2.50 2.75 3.00"}
DRY=${1:-}
QSUB=/opt/pbs/bin/qsub
mkdir -p pbs/done pbs/logs

NLBL=$(printf 'N%02d' "$NVAL")
for rate in $RATES; do
	safe="results/heldout_drained/${POLICY}/safe_seeds/${NLBL}_r${rate}.txt"
	if [ ! -s "$safe" ]; then
		echo "skip ${NLBL}/r${rate} (no drained-safe seeds)"; continue
	fi
	deps=""
	for s in $(seq 0 9); do
		SIDX=$(printf "%02d" "$s")
		NAME="dl_cof_${POLICY}_${NLBL}r${rate}_s${SIDX}"
		if [ -f "pbs/done/${NAME}.done" ]; then
			echo "skip ${NAME} (done)"; continue
		fi
		CMD="STALL_POLICY=${POLICY} UNION_POOL=full python3 run_coreach_drained.py cell --N ${NVAL} --rate ${rate} --slice ${s}"
		if [ "$DRY" = "--dry-run" ]; then echo "would submit ${NAME}"; continue; fi
		JID=$($QSUB -N "${NAME}" -v RUNCMD="${CMD}" pbs/cell.pbs)
		echo "submitted ${NAME} ${JID}"
		deps="${deps}:${JID}"
	done
	MNAME="dl_cof_${POLICY}_${NLBL}r${rate}_merge"
	MCMD="STALL_POLICY=${POLICY} UNION_POOL=full python3 run_coreach_drained.py merge --N ${NVAL} --rate ${rate}"
	if [ "$DRY" = "--dry-run" ]; then echo "would submit ${MNAME}"; continue; fi
	if [ -n "$deps" ]; then
		$QSUB -N "${MNAME}" -W "depend=afterok${deps}" -v RUNCMD="${MCMD}" pbs/cell.pbs \
			| sed "s/^/submitted ${MNAME} /"
	else
		$QSUB -N "${MNAME}" -v RUNCMD="${MCMD}" pbs/cell.pbs | sed "s/^/submitted ${MNAME} /"
	fi
done
