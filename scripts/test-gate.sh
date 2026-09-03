#!/usr/bin/env bash
# The push gate as a standalone, runnable-by-hand step: run `make test-fast` on
# the current tree and stamp the result so the next push is instant.
#
#   make test-gate            # or: ./scripts/test-gate.sh
#   dualgit ship "fix: ..."   # pre-push sees the stamp and pushes immediately
#
# WHY THIS EXISTS AS A SCRIPT AND NOT ONLY INSIDE THE pre-push HOOK
# (measured 2026-09-04): git opens the SSH connection to the remote BEFORE it
# runs pre-push. On a loaded box the in-hook gate took 34 minutes, GitHub closed
# the idle connection, and a *green* gate still failed to push (SIGPIPE, exit
# 141). Running the gate first — this script — and then pushing keeps the SSH
# window to seconds. The pre-push hook calls this same script, so the stamp,
# digest, and behavior cannot drift between the two paths.
#
# Result caching: `dualgit ship` pushes BOTH gitdirs; the stamp records the
# digest of every tracked and untracked .py file so one identical tree pays the
# suite once. The stamp is pinned to .git (public) on purpose: one tree, one
# digest, one stamp.
set -uo pipefail
cd "$(dirname "$0")/.."

DIGEST=$(find src tests scripts -name '*.py' -type f -print0 2>/dev/null \
         | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1)
STAMP=".git/nova-test-gate-passed"

if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$DIGEST" ]; then
  echo "test-gate: already GREEN for this exact tree (stamp matches) — nothing to run" >&2
  exit 0
fi

# Gate-width workers: the gate is user-initiated and its wall-clock is what the
# user waits on, so it gets a floor of half the cores even on a loaded box —
# `nice` keeps it polite on CPU, and the memory cap still applies because swap,
# not CPU, is what actually kills this machine. A background Stop-hook run gets
# no such floor.
export PYTEST_XDIST_AUTO_NUM_WORKERS="${PYTEST_XDIST_AUTO_NUM_WORKERS:-$(./scripts/test-workers.sh --gate)}"
echo "test-gate: running make test-fast with $PYTEST_XDIST_AUTO_NUM_WORKERS workers" >&2

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
if env -u FORCE_COLOR -u COLORTERM -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE \
     nice -n 10 make test-fast >&2; then
  echo "$DIGEST" > "$STAMP"
  echo "test-gate: GREEN — stamped; the next push of this tree skips the re-run" >&2
  exit 0
fi

echo "test-gate: FAILED — the tree is red, nothing was stamped" >&2
exit 1
