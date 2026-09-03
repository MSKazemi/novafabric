#!/usr/bin/env bash
# Run only the tests a change can affect. See scripts/testsel.py for the selection
# rules and, importantly, for what static selection cannot see.
#
#   scripts/run-scoped-tests.sh direct    # Tier 0 — seconds, runs on every edit
#   scripts/run-scoped-tests.sh impact    # Tier 1 — import closure, pre-commit
#
# A selection of "*" means the change is too broad to scope, and the full fast
# suite runs instead. Escalating is always the safe direction.
set -uo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-impact}"
shift || true

# ANSI escapes break ~88 CLI/JSON assertions in this suite.
unset FORCE_COLOR COLORTERM

SEL=$(uv run python scripts/testsel.py --mode "$MODE" 2>/tmp/testsel.err)
REASON=$(cat /tmp/testsel.err)

if [ -z "$SEL" ]; then
  echo "scoped tests: nothing to run — $REASON"
  exit 0
fi

if [ "$SEL" = "*" ]; then
  echo "scoped tests: escalating to the full fast suite — $REASON"
  exec uv run pytest -n auto --dist=loadgroup --benchmark-disable -q \
       -m "not container" --ignore=tests/integration "$@"
fi

COUNT=$(echo "$SEL" | wc -l)
echo "scoped tests: $REASON"
# shellcheck disable=SC2086
exec uv run pytest -n auto --dist=loadgroup --benchmark-disable -q \
     -m "not container" -p no:randomly $SEL "$@"
