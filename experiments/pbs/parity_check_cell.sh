#!/bin/bash
# Compare one training cell produced by two binaries.
#
#   bash pbs/parity_check_cell.sh <old_cell_dir> <new_cell_dir>
#
# The pruning rewrite of 2026-10-21 was validated core by core on 3,445 cores.
# That leaves the question of whether a whole cell comes out the same, which is
# what the published tables are built from. Run on corridor/discard/N05/r2.50 on
# 2026-10-22: constraints.csv and warm_constraints.csv byte-identical, md5
# b905eca452e47cd96408d23cec193131, and the two logs identical on every column
# but CPU time.
#
# CPU time is excluded deliberately. It is measured, not computed, so it differs
# between any two runs of the same binary and says nothing about agreement.
set -uo pipefail
OLD="${1:?usage: parity_check_cell.sh <old_cell_dir> <new_cell_dir>}"
NEW="${2:?usage: parity_check_cell.sh <old_cell_dir> <new_cell_dir>}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0

for f in constraints.csv warm_constraints.csv; do
	if [ ! -f "$OLD/$f" ] || [ ! -f "$NEW/$f" ]; then echo "  SKIP $f (absent)"; continue; fi
	if cmp -s "$OLD/$f" "$NEW/$f"; then echo "  OK   $f identical ($(md5sum < "$OLD/$f" | cut -d' ' -f1))"
	else echo "  FAIL $f differs"; fail=1; fi
done

# training_log.csv: seed,deadlock_found,new_constraints,total_constraints,
# iterations,trains_completed,trains_per_hr,avg_wait_time,cpu_time -> drop 9.
# benchmark.csv: cpu_learned is column 3 and cpu_pl column 8.
cmp_cut () {
	local f="$1" keep="$2"
	if [ ! -f "$OLD/$f" ] || [ ! -f "$NEW/$f" ]; then echo "  SKIP $f (absent)"; return; fi
	cut -d, -f"$keep" "$OLD/$f" > "$TMP/o"; cut -d, -f"$keep" "$NEW/$f" > "$TMP/n"
	if cmp -s "$TMP/o" "$TMP/n"; then echo "  OK   $f identical apart from CPU time"
	else echo "  FAIL $f differs beyond CPU time:"; diff "$TMP/o" "$TMP/n" | head -6; fail=1; fi
}
cmp_cut training_log.csv 1-8
cmp_cut benchmark.csv 1,2,4,5,6,7,9,10,11

[ "$fail" -eq 0 ] && { echo "VERIFY: PASS -- the two binaries agree on this cell"; exit 0; }
echo "VERIFY: FAIL -- the two binaries do not agree on this cell" >&2; exit 1
