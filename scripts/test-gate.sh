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
# Result caching: `dualgit ship` pushes BOTH gitdirs; the stamp records a digest
# of the tree so one identical tree pays the suite once. The stamp is pinned to
# .git (public) on purpose: one tree, one digest, one stamp.
#
# ⚠ THE DIGEST MUST COVER EVERY INPUT THE SUITE READS, NOT JUST THE CODE.
# It used to be `find src tests scripts -name '*.py'` — Python sources only.
# That silently excluded almost everything the suite actually asserts against:
# pyproject.toml and uv.lock (dependency changes — the highest-risk class of
# all), collector/ (Go), web/ and packages/ (TypeScript), schemas/, the
# Makefile, .github/workflows/, every non-.py fixture, and the whole of docs/.
#
# The ~186 guards in tests/docs read exactly those files. So a docs-only or
# lockfile-only change left the digest untouched, the stamp still matched, and
# this script printed "already GREEN for this exact tree" about a tree the suite
# had never seen in that state. Measured 2026-09-04: after changing
# collector/go.mod, collector/go.sum and CHANGELOG.md, the old digest was
# byte-identical to the stamp and the gate skipped the run entirely.
#
# Hashing every tracked and untracked-but-not-ignored file instead costs 0.58 s
# over 4342 files, so the narrow key bought nothing that mattered.
set -uo pipefail
cd "$(dirname "$0")/.."

# GIT_DIR is unset here for the same reason it is unset around `make test-fast`
# below: git exports it to hooks, so the private pre-push would otherwise
# enumerate the PRIVATE index (17.9k files incl. design/, papers/, .claude/) and
# compute a different digest than the public push for one identical tree. That
# would quietly break the "one tree, one digest, one stamp" invariant above.
_tree_files() {
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE sh -c '
    git ls-files -z
    git ls-files -z --others --exclude-standard
  ' 2>/dev/null | sort -zu
}

# `--print-files` exists so the guard in tests/docs can assert what the digest
# actually covers. It prints the very list the digest is computed from below, so
# a future narrowing cannot pass the guard while changing the real cache key.
if [ "${1:-}" = "--print-files" ]; then
  _tree_files | tr '\0' '\n'
  exit 0
fi

DIGEST=$(_tree_files | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1)
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
