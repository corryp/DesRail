#!/bin/bash
# Re-evaluate the two scenarios where the rate-agnostic union loses to the
# per-scenario set, using a union that keeps the non-converged rates.
#
# Why these two: under the discarding variant every constraint in a
# non-converged scenario's set came from a replication that drained, so such a
# set is incomplete rather than unreliable, and excluding it from the union only
# removes coverage. At N=20 the exclusion is nearly total, the converged-only
# union holding 3,191 constraints against the 37,080 of the full one, and those
# are exactly the two cells where the union leaves a higher residual than the
# per-scenario set (895 against 506, and 2,156 against 1,542 per 10,000).
#
# 10 slices of 1,000 held-out seeds per cell, then a merge per cell. Slices are
# resumable by seed, so a walltime kill loses nothing: re-run this script and
# only the unfinished slices are submitted again.
#
#   bash pbs/submit_fullunion.sh            # submit what is not already done
#   bash pbs/submit_fullunion.sh --dry-run  # print the jobs and submit nothing
set -euo pipefail
cd "$(dirname "$0")/.."
POLICY=${POLICY:-discard}
CELLS=${CELLS:-"20:2.75 20:3.00"}
DRY=${1:-}
QSUB=/opt/pbs/bin/qsub
mkdir -p pbs/done pbs/logs

for cell in $CELLS; do
	N=${cell%%:*}; RATE=${cell##*:}
	NLBL=$(printf "N%02d" "$N")
	deps=""
	for s in $(seq 0 9); do
		SIDX=$(printf "%02d" "$s")
		NAME="dl_fu_${POLICY}_${NLBL}r${RATE}_s${SIDX}"
		if [ -f "pbs/done/${NAME}.done" ]; then
			echo "skip ${NAME} (done)"
			continue
		fi
		CMD="STALL_POLICY=${POLICY} python3 run_heldout_drained.py cell --N ${N} --rate ${RATE} --mode fullunion --slice ${s}"
		if [ "$DRY" = "--dry-run" ]; then
			echo "would submit ${NAME}: ${CMD}"
			continue
		fi
		JID=$($QSUB -N "${NAME}" -v RUNCMD="${CMD}" pbs/cell.pbs)
		echo "submitted ${NAME} ${JID}"
		deps="${deps}:${JID}"
	done
	MNAME="dl_fu_${POLICY}_${NLBL}r${RATE}_merge"
	MCMD="STALL_POLICY=${POLICY} python3 run_heldout_drained.py merge --N ${N} --rate ${RATE} --mode fullunion"
	if [ "$DRY" = "--dry-run" ]; then
		echo "would submit ${MNAME} after${deps:-<nothing>}"
		continue
	fi
	if [ -n "$deps" ]; then
		$QSUB -N "${MNAME}" -W "depend=afterok${deps}" -v RUNCMD="${MCMD}" pbs/cell.pbs \
			| sed "s/^/submitted ${MNAME} /"
	else
		$QSUB -N "${MNAME}" -v RUNCMD="${MCMD}" pbs/cell.pbs | sed "s/^/submitted ${MNAME} /"
	fi
done
