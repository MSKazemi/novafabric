#!/bin/sh
# run_demo.sh — end-to-end blackbox recorder demo (POSIX sh, no bash-isms)
# All 8 steps: capture bad run, validate, scan-secrets, replay, capture fixed
# run, diff, lineage, and verify (offline; sealing contacts a TSA only if
# novaseal.yaml sets tsa_url).
#
# Usage:
#   cd /path/to/novafabric
#   sh examples/blackbox_demo/run_demo.sh
#
# The script starts mock_llm_server.py in the background and stops it on exit.
# Set SKIP_VERIFY=1 to skip Step 8 (nova verify) in airgapped environments.
# Capsules go to $NOVAFABRIC_CAPSULE_DIR, else $NOVAFABRIC_HOME/capsules, else
# ~/.novafabric/capsules — the same default `nova capture` uses — passed to
# `nova capture --output-dir` explicitly so the capsule path is known exactly.
# Set PYTHON to choose the interpreter (default: python3, else python).
# Set DEMO_PORT to move the mock LLM server off port 9099.
set -eu

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
NOVA="${NOVA:-nova}"
PYTHON="${PYTHON:-$(command -v python3 || command -v python || echo python)}"
DEMO_PORT="${DEMO_PORT:-9099}"
export DEMO_PORT
CAPSULE_BASE="${NOVAFABRIC_CAPSULE_DIR:-${NOVAFABRIC_HOME:-$HOME/.novafabric}/capsules}"
# shellcheck source=capsule_path.sh
. "$DEMO_DIR/capsule_path.sh"

# ── helpers ────────────────────────────────────────────────────────────────
sep() { printf '\n────────────────────────────────────────────────────────────────────\n'; }
step() { sep; printf 'STEP %s — %s\n' "$1" "$2"; sep; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

# ── mock server ────────────────────────────────────────────────────────────
step "0" "Starting mock LLM server on http://127.0.0.1:$DEMO_PORT"
"$PYTHON" "$DEMO_DIR/mock_llm_server.py" &
MOCK_PID=$!
# register cleanup handler before anything else can fail
trap 'kill "$MOCK_PID" 2>/dev/null; true' EXIT INT TERM

# wait (up to ~10 s) for the server to accept connections
_tries=0
until "$PYTHON" -c "import socket,sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), 0.5).close()" "$DEMO_PORT" 2>/dev/null; do
    _tries=$((_tries + 1))
    test "$_tries" -lt 50 || die "mock LLM server did not start on port $DEMO_PORT"
    sleep 0.2
done

export OPENAI_API_KEY=sk-demo-no-key-needed
export OPENAI_BASE_URL="http://127.0.0.1:$DEMO_PORT"
export NOVAFABRIC_SUGGEST=0

# ── step 1: capture bad run ────────────────────────────────────────────────
step "1" "Capture the bad run (agent recommends disabling rate-limiting)"
BAD_CAPTURE=$("$NOVA" capture --output-dir "$CAPSULE_BASE" -- "$PYTHON" "$DEMO_DIR/agent.py" --mode bad 2>&1 | tee /dev/stderr)
BAD_RUN=$(capsule_path "$CAPSULE_BASE" "$BAD_CAPTURE") \
    || die "No capsule found: nova capture printed no run_id, or $CAPSULE_BASE/<run_id> does not exist"
printf 'BAD_RUN=%s\n' "$BAD_RUN"

# verify decision.json was written
test -f outputs/decision.json || die "outputs/decision.json not found after bad run"
printf 'outputs/decision.json contents:\n'
cat outputs/decision.json

# ── step 2: validate capsule ───────────────────────────────────────────────
step "2" "Validate capsule schema"
"$NOVA" validate "$BAD_RUN"

# verify at least one model-call record was captured
MODEL_CALLS="$BAD_RUN/model-calls.jsonl"
test -f "$MODEL_CALLS" || die "model-calls.jsonl not found in capsule"
CALL_COUNT=$(wc -l < "$MODEL_CALLS")
test "$CALL_COUNT" -ge 1 || die "Expected ≥1 model-call record, got $CALL_COUNT"
printf 'model-calls.jsonl: %d record(s) captured\n' "$CALL_COUNT"

# ── step 3: scan secrets ───────────────────────────────────────────────────
step "3" "Scan capsule for secrets / redaction proof"
"$NOVA" scan-secrets "$BAD_RUN"

# ── step 4: replay ────────────────────────────────────────────────────────
step "4" "Forensic replay (read-only inspection, no subprocess)"
"$NOVA" replay "$BAD_RUN" --mode forensic

# ── step 5: capture fixed run ──────────────────────────────────────────────
step "5" "Capture the fixed run (agent recommends reducing max_connections)"
FIXED_CAPTURE=$("$NOVA" capture --output-dir "$CAPSULE_BASE" -- "$PYTHON" "$DEMO_DIR/agent.py" --mode fixed 2>&1 | tee /dev/stderr)
FIXED_RUN=$(capsule_path "$CAPSULE_BASE" "$FIXED_CAPTURE") \
    || die "No capsule found: nova capture printed no run_id, or $CAPSULE_BASE/<run_id> does not exist"
printf 'FIXED_RUN=%s\n' "$FIXED_RUN"

# ── step 6: diff ──────────────────────────────────────────────────────────
step "6" "Diff bad run → fixed run (model response + decision.json changed)"
"$NOVA" diff "$BAD_RUN" "$FIXED_RUN"

# ── step 7: lineage ───────────────────────────────────────────────────────
step "7" "Lineage — provenance of the bad run"
BAD_RUN_ID=$(basename "$BAD_RUN")
"$NOVA" lineage provenance "$BAD_RUN_ID"

# ── step 8: verify (offline) ───────────────────────────────────────────────
if [ "${SKIP_VERIFY:-0}" = "1" ]; then
    step "8" "SKIPPED — nova verify (set SKIP_VERIFY=0 to enable)"
else
    step "8" "Verify cryptographic seal (DSSE + RFC 3161 + Merkle, offline)"
    "$NOVA" verify "$BAD_RUN" || printf 'NOTE: verify needs a sealed capsule (NovaSeal configured at capture); see README\n'
fi

sep
printf 'DEMO COMPLETE\n'
printf '  BAD_RUN:   %s\n' "$BAD_RUN"
printf '  FIXED_RUN: %s\n' "$FIXED_RUN"
sep
