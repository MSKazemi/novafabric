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

# Size `-n auto` to the cores/memory that are actually free, and run niced so
# an automated QA pass never outcompetes interactive work. See scripts/test-workers.sh.
export PYTEST_XDIST_AUTO_NUM_WORKERS="${PYTEST_XDIST_AUTO_NUM_WORKERS:-$(./scripts/test-workers.sh)}"

# .venv/bin directly, not `uv run`: uv serializes on a project-wide lock that
# every concurrent session contends for. Automated runs must never queue on it;
# the uv fallback only exists for a tree whose venv is not built yet.
PY=".venv/bin/python"
[ -x "$PY" ] || PY="uv run python"
PYTEST=".venv/bin/pytest"
[ -x "$PYTEST" ] || PYTEST="uv run pytest"

SEL=$($PY scripts/testsel.py --mode "$MODE" 2>/tmp/testsel.err)
REASON=$(cat /tmp/testsel.err)

if [ -z "$SEL" ]; then
  echo "scoped tests: nothing to run — $REASON"
  exit 0
fi

if [ "$SEL" = "*" ]; then
  echo "scoped tests: escalating to the full fast suite — $REASON"
  exec nice -n 10 $PYTEST -n auto --dist=loadgroup --benchmark-disable -q \
       -m "not container" --ignore=tests/integration "$@"
fi

COUNT=$(echo "$SEL" | wc -l)
echo "scoped tests: $REASON"
# shellcheck disable=SC2086
exec nice -n 10 $PYTEST -n auto --dist=loadgroup --benchmark-disable -q \
     -m "not container" -p no:randomly $SEL "$@"
