#!/bin/bash
# Per-train delay extraction over the full 10,000 held-out replications, for the
# distribution figure. Ten slices of 1,000 replications per cell, then a merge.
#
#   bash pbs/submit_train_delays.sh            # submit what is not already done
#   bash pbs/submit_train_delays.sh --dry-run
set -euo pipefail
cd "$(dirname "$0")/.."
POLICY=${POLICY:-discard}
NVAL=${NVAL:-10}
RATES=${RATES:-"1.00 1.50 2.00 2.50 3.00"}
MODES=${MODES:-"native engineered"}
DRY=${1:-}
QSUB=/opt/pbs/bin/qsub
mkdir -p pbs/done pbs/logs

for rate in $RATES; do
	for mode in $MODES; do
		short=$([ "$mode" = native ] && echo nat || echo eng)
		deps=""
		for s in $(seq 0 9); do
			SIDX=$(printf "%02d" "$s")
			NAME="dl_td_${POLICY}_${short}_N$(printf '%02d' "$NVAL")r${rate}_s${SIDX}"
			if [ -f "pbs/done/${NAME}.done" ]; then
				echo "skip ${NAME} (done)"; continue
			fi
			CMD="STALL_POLICY=${POLICY} python3 extract_train_delays.py slice --N ${NVAL} --rate ${rate} --mode ${mode} --slice ${s}"
			if [ "$DRY" = "--dry-run" ]; then echo "would submit ${NAME}"; continue; fi
			JID=$($QSUB -N "${NAME}" -v RUNCMD="${CMD}" pbs/cell.pbs)
			echo "submitted ${NAME} ${JID}"
			deps="${deps}:${JID}"
		done
		MNAME="dl_td_${POLICY}_${short}_N$(printf '%02d' "$NVAL")r${rate}_merge"
		MCMD="STALL_POLICY=${POLICY} python3 extract_train_delays.py merge --N ${NVAL} --rate ${rate} --mode ${mode}"
		if [ "$DRY" = "--dry-run" ]; then echo "would submit ${MNAME}"; continue; fi
		if [ -n "$deps" ]; then
			$QSUB -N "${MNAME}" -W "depend=afterok${deps}" -v RUNCMD="${MCMD}" pbs/cell.pbs \
				| sed "s/^/submitted ${MNAME} /"
		else
			$QSUB -N "${MNAME}" -v RUNCMD="${MCMD}" pbs/cell.pbs | sed "s/^/submitted ${MNAME} /"
		fi
	done
done
