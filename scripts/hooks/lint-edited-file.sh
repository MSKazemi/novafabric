#!/usr/bin/env bash
# PostToolUse(Write|Edit): lint just the file that was edited.
#
# Tier 0a of the test strategy — the cheapest possible feedback, on every edit.
# Exit 2 makes Claude Code feed stderr back to the model, so a lint error is
# corrected in the same turn instead of surfacing minutes later in a gate.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0

FILE=$(jq -r '.tool_response.filePath // .tool_input.file_path // empty' 2>/dev/null)
[ -z "$FILE" ] && exit 0
case "$FILE" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$FILE" ] || exit 0

# .venv/bin directly, not `uv run`: uv serializes on a project-wide lock that
# every concurrent session contends for (documented deadlock signature: zero
# CPU across siblings). A per-edit hook must never queue on it.
RUFF=".venv/bin/ruff"
[ -x "$RUFF" ] || RUFF="uv run ruff"
OUT=$($RUFF check "$FILE" 2>&1)
STATUS=$?
if [ $STATUS -ne 0 ]; then
  echo "ruff found problems in $FILE — fix them now:" >&2
  echo "$OUT" >&2
  exit 2
fi
exit 0
