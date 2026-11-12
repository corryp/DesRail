#!/bin/bash
# Re-benchmark the engineered spawn-hold arm of the overlength experiment with
# the 6N constraint set, replacing the 2026-10-22 campaign's 14N-4 set.
#
# The 8N-4 lock-through forms were measured inert (1,500 paired seeds identical
# on deadlock flag, detection time and throughput), but the paper reports 6N, so
# the benchmark it reports should have run 6N. The campaign's files are kept as
# engineered_benchmark_lockthrough.csv rather than deleted, because they are the
# evidence for the inertness claim.
#
# Phase 4 only: no retraining, no PL or hybrid re-run.
#
#   bash pbs/submit_overlength_eng6n.sh
#   bash pbs/submit_overlength_eng6n.sh --dry-run
set -euo pipefail
cd "$(dirname "$0")/.."
POLICIES=${POLICIES:-"discard bank"}
NVALS=${NVALS:-"2 5 10 15 20"}
FRACS=${FRACS:-"0.10 0.25 0.50"}
DRY=${1:-}
QSUB=/opt/pbs/bin/qsub
mkdir -p pbs/done pbs/logs

for pol in $POLICIES; do
	for N in $NVALS; do
		NLBL=$(printf 'N%02d' "$N")
		for f in $FRACS; do
			cell="work/overlength/${pol}/${NLBL}/f${f}"
			if [ ! -d "$cell" ]; then
				echo "skip ${pol}/${NLBL}/f${f} (no cell)"; continue
			fi
			# Set the 14N-4 result aside, once, so the fresh run starts empty.
			# The runner resumes from whatever rows are already there, so a
			# leftover file would be mistaken for a finished 6N benchmark.
			if [ -f "${cell}/engineered_benchmark.csv" ]; then
				if [ "$DRY" = "--dry-run" ]; then
					echo "would move ${cell}/engineered_benchmark.csv aside"
				else
					mv -n "${cell}/engineered_benchmark.csv" \
					      "${cell}/engineered_benchmark_lockthrough.csv"
					rm -f "${cell}/engineered_benchmark.csv"
				fi
			fi
			NAME="dl_o6n_${pol}_${NLBL}f${f}"
			rm -f "pbs/done/${NAME}.done"
			CMD="STALL_POLICY=${pol} python3 runners/run_overlength.py --phase engineered --jobs 1 --scenarios ${NLBL}:f${f}"
			if [ "$DRY" = "--dry-run" ]; then echo "would submit ${NAME}"; continue; fi
			$QSUB -N "${NAME}" -v RUNCMD="${CMD}" pbs/cell.pbs \
				| sed "s/^/submitted ${NAME} /"
		done
	done
done
