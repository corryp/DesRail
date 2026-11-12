#!/bin/bash
# Assert that one binary produced every cell of a campaign.
#
#   bash pbs/check_exe_identity.sh            (from the bundle directory)
#
# cell.pbs records the md5 of the binary it ran in each cell log. A published
# campaign has to be provable, not merely believed, to have used one pruner
# throughout: the campaign this one replaces was started on 19 October with a
# binary built before the pruning rewrite, and nothing in its output said so.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS="$HERE/logs"
[ -d "$LOGS" ] || { echo "no $LOGS yet"; exit 0; }
mapfile -t seen < <(grep -h '^\[job\] exe_md5=' "$LOGS"/*.log 2>/dev/null | sed 's/.*=//' | sort -u)
missing=$(grep -L '^\[job\] exe_md5=' "$LOGS"/*.log 2>/dev/null | wc -l)
echo "cell logs: $(ls "$LOGS"/*.log 2>/dev/null | wc -l), distinct binaries: ${#seen[@]}, logs with no md5: $missing"
for m in "${seen[@]}"; do echo "  $m  $(grep -l "exe_md5=$m" "$LOGS"/*.log | wc -l) cells"; done
if [ "${#seen[@]}" -gt 1 ]; then
	echo "VERIFY: FAIL -- more than one binary across the campaign" >&2; exit 1
fi
# Logs predating this check carry no md5 line, so they cannot be cleared by it.
if [ "$missing" -gt 0 ]; then
	echo "VERIFY: FAIL -- $missing cell logs do not record a binary" >&2; exit 1
fi
echo "VERIFY: PASS -- one binary, ${seen[0]}, across every cell log"
