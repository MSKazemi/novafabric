#!/usr/bin/env bash
# Print the number of pytest-xdist workers a test run should use RIGHT NOW.
#
# `-n auto` claims every core unconditionally; with several sessions and repos
# running suites at once this machine has been measured at load 49–65 on 20
# cores with 11 GiB in swap. pytest-xdist honours PYTEST_XDIST_AUTO_NUM_WORKERS
# as the value of `auto`, so the Makefile and the scoped-test runner export the
# output of this script — the pytest command lines never change, which keeps
# `test-par` byte-for-byte identical to CI.
#
#   NOVA_TEST_WORKERS=8 make test-fast   # explicit override, wins over everything
#   scripts/test-workers.sh --gate       # user-initiated gate run: floor of nproc/2
#
# Policy: leave the cores that are already busy alone, never exceed what
# available memory can feed (a worker peaks around ~800 MB on this suite), and
# always grant at least 1 so a run can make progress even on a saturated box.
#
# --gate: a push gate is something a person is WAITING on, so its wall-clock
# matters more than politeness — it gets at least half the cores (nice -n 10
# keeps it yielding on CPU). The memory cap still binds: swap, not CPU, is what
# actually kills this machine, and a serial 12K-test gate (measured: 34 min)
# is worse than a briefly oversubscribed one.
set -uo pipefail

GATE=0
[ "${1:-}" = "--gate" ] && GATE=1

if [ -n "${NOVA_TEST_WORKERS:-}" ]; then
  case "$NOVA_TEST_WORKERS" in
    *[!0-9]*|'') echo "test-workers: NOVA_TEST_WORKERS='$NOVA_TEST_WORKERS' is not a number" >&2; exit 1 ;;
    *) echo "$NOVA_TEST_WORKERS"; exit 0 ;;
  esac
fi

NPROC=$(nproc 2>/dev/null || echo 4)

# Cores not already occupied: nproc minus the 1-minute load average, rounded.
LOAD1=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo 0)
LOAD_INT=$(printf '%.0f' "$LOAD1" 2>/dev/null || echo 0)
CPU_FREE=$((NPROC - LOAD_INT))

# Memory headroom: MemAvailable is what the kernel says can be claimed without
# swapping; budget ~800 MB per worker (measured worker RSS on this suite is
# 0.5–1.8 GB, most under 800 MB).
AVAIL_KB=$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
MEM_WORKERS=$((AVAIL_KB / 800 / 1024))

N=$CPU_FREE
if [ "$GATE" = 1 ]; then
  FLOOR=$((NPROC / 2))
  [ "$N" -lt "$FLOOR" ] && N=$FLOOR
fi
[ "$MEM_WORKERS" -lt "$N" ] && N=$MEM_WORKERS
[ "$N" -gt "$NPROC" ] && N=$NPROC
[ "$N" -lt 1 ] && N=1

echo "$N"
