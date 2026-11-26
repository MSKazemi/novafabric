#!/usr/bin/env bash
# Run a command while holding this machine's test-pool lock: one parallel pytest
# pool per user at a time, whichever session, worktree or isolated HOME starts it.
#
#   ./scripts/test-pool.sh uv run pytest -n auto ...
#
# WHY. Two full xdist pools at once have ended in SIGKILL and a reboot on this class
# of machine, and concurrent sessions kept starting them because nothing told one
# session that another was mid-run. The load-aware worker count (test-workers.sh)
# sizes one pool; this lock stops the second one from starting. A waiting run says
# who holds the lock and then waits its turn.
#
#   NOVA_TEST_POOL_WAIT=<seconds>  give up after that long and exit 75 (EX_TEMPFAIL);
#                                  empty (the default) waits as long as it takes
#   NOVA_TEST_POOL_LOCK=<path>     a different lock file; "off" runs with no lock
#
# The lock is a fixed path under /tmp, not $TMPDIR or $HOME, on purpose: sessions
# and isolated test runs override both, and a lock each run sees privately locks
# nothing. Nested calls (the gate runs `make test-fast`, which runs this) pass
# straight through via NOVA_TEST_POOL_HELD. The command runs with the lock's file
# descriptor closed, so a daemon a test leaves behind cannot keep the lock held.
# Guard: tests/docs/test_test_pool_lock.py.
set -uo pipefail

[ "$#" -gt 0 ] || { echo "usage: $0 <command> [args...]" >&2; exit 2; }

if [ "${NOVA_TEST_POOL_HELD:-}" = "1" ] || [ "${NOVA_TEST_POOL_LOCK:-}" = "off" ]; then
  exec "$@"
fi
if ! command -v flock >/dev/null 2>&1; then
  echo "test-pool: flock not found — running WITHOUT the shared one-pool lock" >&2
  exec "$@"
fi

WAIT="${NOVA_TEST_POOL_WAIT:-}"
case "$WAIT" in
  ''|*[!0-9]*) [ -n "$WAIT" ] && { echo "test-pool: NOVA_TEST_POOL_WAIT='$WAIT' is not a number" >&2; exit 2; } ;;
esac

LOCK="${NOVA_TEST_POOL_LOCK:-/tmp/nova-test-pool-$(id -u).lock}"
HOLDER="$LOCK.holder"
if ! exec 9>>"$LOCK"; then
  echo "test-pool: cannot open $LOCK — running WITHOUT the shared one-pool lock" >&2
  exec "$@"
fi

if ! flock -n 9; then
  echo "test-pool: another test pool is running ($(cat "$HOLDER" 2>/dev/null || echo 'holder unknown')) — waiting for it" >&2
  if [ -n "$WAIT" ]; then
    flock -w "$WAIT" 9 || { echo "test-pool: still busy after ${WAIT}s — giving up" >&2; exit 75; }
  else
    flock 9
  fi
  echo "test-pool: lock acquired, starting" >&2
fi

printf 'pid %s since %s in %s\n' "$$" "$(date '+%F %T')" "$PWD" > "$HOLDER" 2>/dev/null || true
NOVA_TEST_POOL_HELD=1 "$@" 9>&-
STATUS=$?
: > "$HOLDER" 2>/dev/null || true
exit "$STATUS"
