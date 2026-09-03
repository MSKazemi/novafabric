#!/usr/bin/env bash
# pre-push: the real gate. Nothing red leaves this machine.
#
# Tier 2. The gate itself lives in scripts/test-gate.sh (digest → make test-fast
# → stamp) so it can also be run BY HAND before pushing — `make test-gate` — and
# the two paths can never drift. Prefer that: git opens the SSH connection to the
# remote before running this hook, and a long gate run inside the hook can
# outlive it (measured 2026-09-04: 34 min under load, GitHub closed the idle
# connection, and a GREEN gate still failed to push with SIGPIPE/exit 141).
#
# Escape hatch (documented, deliberate): NOVA_SKIP_TEST_GATE=1 git push ...
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0

[ "${NOVA_SKIP_TEST_GATE:-}" = "1" ] && {
  echo "pre-push: test gate SKIPPED by NOVA_SKIP_TEST_GATE=1" >&2; exit 0; }

START=$SECONDS
if ./scripts/test-gate.sh; then
  # If the suite actually ran (rather than hitting the stamp), the SSH
  # connection may already be dead even though the gate is green. Say so —
  # the stamp makes the retry instant, but only if the user knows to retry.
  if [ $((SECONDS - START)) -ge 120 ]; then
    cat >&2 <<'MSG'
pre-push: gate GREEN after a long run. If this push now fails with
"connection closed by remote host", the remote hung up while the gate ran —
just push again: the result is stamped and the re-run is skipped.
(Next time: `make test-gate` first, then push.)
MSG
  fi
  exit 0
fi

cat >&2 <<'MSG'

pre-push: TEST GATE FAILED — push refused.

Fix the failures (re-check with `make test-gate`), or bypass deliberately with:
    NOVA_SKIP_TEST_GATE=1 git push ...
MSG
exit 1
