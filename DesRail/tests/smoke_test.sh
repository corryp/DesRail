#!/bin/bash
#
# Smoke test for the DesRail simulator: run against a config directory and
# verify it exits cleanly within a time budget, producing some output.
#
# Usage:   tests/smoke_test.sh <path-to-DesRail-exe> <path-to-config-dir> [timeout-seconds]
# Example: tests/smoke_test.sh ./build/DesRail \
#              ../experiments/saved_results/2026-02-26_deadlock_exp/experiments/configs/N02
#
# The config dir must contain an `input/` subdirectory with the standard CSVs.
# Exits 0 on success; any non-zero exit code means the simulator crashed,
# timed out, or produced no output.

set -u
EXE=${1:?usage: $0 <exe> <config-dir> [timeout]}
CFG=${2:?usage: $0 <exe> <config-dir> [timeout]}
TMO=${3:-120}

if [ ! -x "$EXE" ]; then
    echo "FAIL: $EXE not executable" >&2
    exit 1
fi
if [ ! -d "$CFG/input" ]; then
    echo "FAIL: $CFG/input not found" >&2
    exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp -r "$CFG/input" "$WORK/"
mkdir -p "$WORK/output"

( cd "$WORK" && timeout "$TMO" "$EXE" ) > "$WORK/smoke.log" 2>&1
rc=$?

if [ $rc -ne 0 ]; then
    echo "FAIL: exit code $rc" >&2
    tail -30 "$WORK/smoke.log" >&2
    exit 1
fi
if [ ! -s "$WORK/smoke.log" ]; then
    echo "FAIL: simulator produced no output" >&2
    exit 1
fi

echo "PASS: $(wc -l < "$WORK/smoke.log") lines of output in ${TMO}s budget"
