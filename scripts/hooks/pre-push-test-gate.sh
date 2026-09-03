#!/usr/bin/env bash
# pre-push: the real gate. Nothing red leaves this machine.
#
# Tier 2. Runs `make test-fast` — the parallel, no-Docker, no-coverage suite
# (~4 min, 12.2K tests). The scoped Stop-hook tier is a fast pre-check that can
# miss string-referenced entrypoints; this one cannot.
#
# Result caching: `dualgit ship` pushes BOTH gitdirs, which would otherwise pay
# the gate twice for one identical tree. The gate records the digest of every
# tracked and untracked .py file it passed on and skips a repeat run on an
# unchanged tree.
#
# Escape hatch (documented, deliberate): NOVA_SKIP_TEST_GATE=1 git push ...
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0

[ "${NOVA_SKIP_TEST_GATE:-}" = "1" ] && {
  echo "pre-push: test gate SKIPPED by NOVA_SKIP_TEST_GATE=1" >&2; exit 0; }

DIGEST=$(find src tests scripts -name '*.py' -type f -print0 2>/dev/null \
         | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1)
STAMP=".git/nova-test-gate-passed"

if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$DIGEST" ]; then
  echo "pre-push: test gate already green for this exact tree — skipping the re-run" >&2
  exit 0
fi

echo "pre-push: running the test gate (make test-fast, ~4 min). NOVA_SKIP_TEST_GATE=1 to bypass." >&2
if env -u FORCE_COLOR -u COLORTERM make test-fast >&2; then
  echo "$DIGEST" > "$STAMP"
  echo "pre-push: test gate GREEN" >&2
  exit 0
fi

cat >&2 <<'MSG'

pre-push: TEST GATE FAILED — push refused.

Fix the failures, or bypass deliberately with:
    NOVA_SKIP_TEST_GATE=1 git push ...
MSG
exit 1
