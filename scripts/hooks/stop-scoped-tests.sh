#!/usr/bin/env bash
# Stop: run the tests the turn's changes can affect, once the turn is over.
#
# Tier 0b. This is the "enterprise developer" part — nobody types a test command;
# the harness runs the right subset at the right moment and hands failures back to
# the model, which fixes them before the turn really ends.
#
# Why Stop and not PostToolUse: PostToolUse fires per edit, so a 17 s test run would
# be paid many times per turn. Stop fires once.
#
# LOOP GUARD. A Stop hook that returns decision:block restarts the model, so a
# failure it cannot fix would loop forever. Each session may be blocked at most
# MAX_BLOCKS times; after that the result is reported without blocking and the
# git pre-push hook remains the real gate.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0

MAX_BLOCKS=2
INPUT=$(cat)
SESSION=$(jq -r '.session_id // "nosession"' <<<"$INPUT" 2>/dev/null)
STATE_DIR="${TMPDIR:-/tmp}/nova-stop-guard"
mkdir -p "$STATE_DIR"
COUNT_FILE="$STATE_DIR/$SESSION"
COUNT=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)

# Fast path: nothing Python changed, so there is nothing to run.
# Uses the PRIVATE gitdir — it tracks the full superset, whereas the public .git
# hides private paths behind .git/info/exclude and would miss a private-only edit.
GITDIR="--git-dir=.git"
[ -d .git-private ] && GITDIR="--git-dir=.git-private"
if ! git $GITDIR status --porcelain 2>/dev/null | grep -qE '\.py$'; then
  exit 0
fi

# LOAD SHED. Several sessions ending turns at once used to stack full-width
# xdist runs (measured 2026-09-04: load 49–65 on 20 cores, 11 GiB in swap).
# If the box is already saturated, running one more suite helps nobody: skip,
# say so, and let pre-push remain the real gate.
NPROC=$(nproc 2>/dev/null || echo 4)
LOAD1=$(printf '%.0f' "$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo 0)")
if [ "$LOAD1" -ge "$NPROC" ]; then
  jq -n --arg m "scoped tests SKIPPED — machine saturated (load $LOAD1 on $NPROC cores); pre-push remains the gate" \
     '{systemMessage:$m}'
  exit 0
fi

# SERIALIZE. One scoped run per repo at a time; a second session waits its turn
# instead of doubling the load, and gives up (without blocking) rather than
# queueing forever.
exec 9>"${STATE_DIR}/scoped-tests.lock"
if ! flock -w 240 9; then
  jq -n '{systemMessage:"scoped tests SKIPPED — another scoped run held the lock for 240s; pre-push remains the gate"}'
  exit 0
fi

OUT=$(./scripts/run-scoped-tests.sh direct 2>&1)
STATUS=$?

if [ $STATUS -eq 0 ]; then
  echo 0 > "$COUNT_FILE"
  SUMMARY=$(grep -oE '[0-9]+ passed[^)]*' <<<"$OUT" | tail -1)
  jq -n --arg m "scoped tests green — ${SUMMARY:-passed}" \
     '{systemMessage:$m, suppressOutput:true}'
  exit 0
fi

TAIL=$(grep -E "^(FAILED|ERROR)|failed" <<<"$OUT" | tail -20)
if [ "$COUNT" -lt "$MAX_BLOCKS" ]; then
  echo $((COUNT + 1)) > "$COUNT_FILE"
  jq -n --arg r "The tests covering your change are FAILING. Fix them before finishing.

$TAIL

(Scoped run; the full gate is 'make test-fast'. Block $((COUNT + 1)) of $MAX_BLOCKS — after that this stops blocking and pre-push becomes the gate.)" \
     '{decision:"block", reason:$r}'
else
  jq -n --arg m "scoped tests still failing after $MAX_BLOCKS attempts — not blocking again; pre-push will gate this" \
     '{systemMessage:$m}'
fi
exit 0
