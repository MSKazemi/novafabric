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
# Pinned to .git (public) on purpose: one tree, one digest, one stamp.
STAMP=".git/nova-test-gate-passed"

if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$DIGEST" ]; then
  echo "pre-push: test gate already green for this exact tree — skipping the re-run" >&2
  exit 0
fi

echo "pre-push: running the test gate (make test-fast, ~4 min). NOVA_SKIP_TEST_GATE=1 to bypass." >&2
# ⚠ GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE are unset deliberately.
#
# git exports GIT_DIR to its hooks. This tree is dual-git, so `git --git-dir=.git-private
# push` runs this gate with GIT_DIR=.git-private, and every test that shells out to
# `git ls-files` then inspects the PRIVATE index — 17.8k files including design/, papers/
# and .claude/ — while asserting properties of the PUBLIC tree. ~25 guards fail at once,
# correctly reporting private paths and the maintainer's home directory as leaks that are
# not leaks. Measured 2026-09-03: the private push could never pass its own gate.
#
# Unsetting them makes the suite see the default .git (public), which is what
# "publicly tracked" means in those guards, and matches how the suite runs everywhere else.
# nice: the gate must finish, not win the scheduler — interactive work and the
# editor stay responsive while it runs. Worker count is already load-aware via
# the Makefile's PYTEST_XDIST_AUTO_NUM_WORKERS export (scripts/test-workers.sh).
if env -u FORCE_COLOR -u COLORTERM -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE \
     nice -n 10 make test-fast >&2; then
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
