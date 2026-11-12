#!/bin/bash
# Stopping-threshold sweep on small corridors (deadlock_paper/revision/R_SWEEP_N2_PLAN.md).
#
#   NS="5 10" bash pbs/submit_r_sweep.sh train   [--dry-run]   (NS defaults to 2)
#   bash pbs/submit_r_sweep.sh train   [--dry-run]   # one job per rate, all R in sequence
#   bash pbs/submit_r_sweep.sh heldout [--dry-run]   # after training: one job per rate, R and chunk
#   bash pbs/submit_r_sweep.sh replay                # after held-out: retrain on R=5000's deadlocked seeds
#   START=1100000 SEEDS=1000000 R_VALUES=5000t bash pbs/submit_r_sweep.sh heldout   # then R5000t on fresh seeds
#   NS="2 5" UP_TO=100000 bash pbs/submit_r_sweep.sh train   # Algorithm 1 at R=100,000, resumed from R=5000
#   NS="2 5" START=10000000 SEEDS=1000000 R_VALUES=100000 bash pbs/submit_r_sweep.sh heldout
#   python3 runners/run_r_sweep.py summary           # after held-out, on the login node
#
# Held-out: SEEDS seeds from 100000 per snapshot, in chunks of CHUNK; raise SEEDS
# (e.g. SEEDS=1000000) and resubmit to extend, since each chunk is its own
# resumable part file. cell.pbs carries the Research Project ID (-P).
set -euo pipefail
cd "$(dirname "$0")/.."
PHASE=${1:?train, heldout or replay}
DRY=${2:-}
NS=${NS:-"2"}
RATES=${RATES:-"1.50 3.00"}
R_VALUES=${R_VALUES:-"100 200 500 1000 2000 5000"}
SEEDS=${SEEDS:-100000}
CHUNK=${CHUNK:-25000}
START=${START:-100000}   # replay snapshots (R_VALUES=5000t) are evaluated from 1100000
UP_TO=${UP_TO:-5000}     # train: largest R; 100000 gets its own job name, as R=5000's are done
QSUB=/opt/pbs/bin/qsub
mkdir -p pbs/done pbs/logs

submit() {
	if [ -f "pbs/done/$1.done" ]; then echo "done already: $1"; return; fi
	if [ "$DRY" = "--dry-run" ]; then echo "would submit $1: $2"; return; fi
	$QSUB -N "$1" -v RUNCMD="$2" pbs/cell.pbs | sed "s/^/submitted $1 /"
}

for N in $NS; do
NN=$(printf "%02d" "$N")
# N=2 keeps the job names it was first submitted under, so its done markers still match.
TAG=$([ "$N" -eq 2 ] && echo "" || echo "_N${NN}")
for rate in $RATES; do
	case "$PHASE" in
	train)
		JSUF=$([ "$UP_TO" -eq 5000 ] && echo "" || echo "_R${UP_TO}")
		submit "rsw_train${TAG}_r${rate}${JSUF}" "python3 runners/run_r_sweep.py train --N ${N} --rate ${rate} --up-to ${UP_TO}"
		;;
	replay)
		# Retrain on the R=5000 set's deadlocked held-out seeds, giving snapshot R5000t.
		submit "rsw_replay${TAG}_r${rate}" "python3 runners/run_r_sweep.py replay --N ${N} --rate ${rate} --from-R 5000 --seeds auto"
		;;
	heldout)
		for R in $R_VALUES; do
			for ((s = START; s < START + SEEDS; s += CHUNK)); do
				submit "rsw_h${TAG}_r${rate}_R${R}_s${s}" \
					"python3 runners/run_r_sweep.py heldout --N ${N} --rate ${rate} --R ${R} --start ${s} --count ${CHUNK}"
			done
		done
		;;
	*) echo "unknown phase $PHASE"; exit 1 ;;
	esac
done
done
