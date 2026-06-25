#!/bin/sh
# run_demo.sh — end-to-end blackbox recorder demo (POSIX sh, no bash-isms)
# All 8 steps: capture bad run, validate, scan-secrets, replay, capture fixed
# run, diff, lineage, and verify (verify requires internet — FreeTSA).
#
# Usage:
#   cd /path/to/novafabric
#   sh examples/blackbox_demo/run_demo.sh
#
# The script starts mock_llm_server.py in the background and stops it on exit.
# Set SKIP_VERIFY=1 to skip Step 8 (nova verify) in airgapped environments.
set -eu

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
NOVA="${NOVA:-nova}"

# ── helpers ────────────────────────────────────────────────────────────────
sep() { printf '\n────────────────────────────────────────────────────────────────────\n'; }
step() { sep; printf 'STEP %s — %s\n' "$1" "$2"; sep; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

# ── mock server ────────────────────────────────────────────────────────────
step "0" "Starting mock LLM server on http://127.0.0.1:9099"
python "$DEMO_DIR/mock_llm_server.py" &
MOCK_PID=$!
# register cleanup handler before anything else can fail
trap 'kill "$MOCK_PID" 2>/dev/null; true' EXIT INT TERM

# brief pause to let the server open the socket
sleep 1

export OPENAI_API_KEY=sk-demo-no-key-needed
export OPENAI_BASE_URL=http://127.0.0.1:9099
export NOVAFABRIC_SUGGEST=0

# ── step 1: capture bad run ────────────────────────────────────────────────
step "1" "Capture the bad run (agent recommends disabling rate-limiting)"
BAD_CAPTURE=$("$NOVA" capture -- python "$DEMO_DIR/agent.py" --mode bad 2>&1 | tee /dev/stderr)
BAD_RUN=$(printf '%s\n' "$BAD_CAPTURE" | grep -oE '[^ ]+\.novafabric/runs/[A-Z0-9]+' | head -1)
test -n "$BAD_RUN" || die "Could not parse capsule path from nova capture output"
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
FIXED_CAPTURE=$("$NOVA" capture -- python "$DEMO_DIR/agent.py" --mode fixed 2>&1 | tee /dev/stderr)
FIXED_RUN=$(printf '%s\n' "$FIXED_CAPTURE" | grep -oE '[^ ]+\.novafabric/runs/[A-Z0-9]+' | head -1)
test -n "$FIXED_RUN" || die "Could not parse capsule path from nova capture output"
printf 'FIXED_RUN=%s\n' "$FIXED_RUN"

# ── step 6: diff ──────────────────────────────────────────────────────────
step "6" "Diff bad run → fixed run (model response + decision.json changed)"
"$NOVA" diff "$BAD_RUN" "$FIXED_RUN"

# ── step 7: lineage ───────────────────────────────────────────────────────
step "7" "Lineage — provenance of the bad run"
BAD_RUN_ID=$(basename "$BAD_RUN")
"$NOVA" lineage provenance "$BAD_RUN_ID"

# ── step 8: verify (requires internet — FreeTSA) ──────────────────────────
if [ "${SKIP_VERIFY:-0}" = "1" ]; then
    step "8" "SKIPPED — nova verify (set SKIP_VERIFY=0 to enable; needs FreeTSA)"
else
    step "8" "Verify cryptographic seal (DSSE + RFC 3161 — needs internet)"
    "$NOVA" verify "$BAD_RUN" || printf 'NOTE: verify needs NovaSeal config; see prerequisites in README\n'
fi

sep
printf 'DEMO COMPLETE\n'
printf '  BAD_RUN:   %s\n' "$BAD_RUN"
printf '  FIXED_RUN: %s\n' "$FIXED_RUN"
sep
