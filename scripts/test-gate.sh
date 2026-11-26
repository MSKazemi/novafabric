#!/usr/bin/env bash
# The push gate as a standalone, runnable-by-hand step: run the tests this tree's
# change needs and stamp the result so the next push is instant.
#
#   make test-gate            # or: ./scripts/test-gate.sh
#   dualgit ship "fix: ..."   # pre-push sees the stamp and pushes immediately
#
# TIERS. `scripts/testsel.py --mode gate` compares the tree with its merge base on
# origin/main and picks one tier (see GATE_TIERS there): none, docs (tests/docs plus
# every test that names a changed path), scoped (that plus the import closure of the
# changed Python), or full (`make test-fast`). Anything it cannot classify is full.
# NOVA_GATE_REQUIRE says what the push accepts:
#   branch  (default; a hand run) every tier
#   main    a push to main: `scoped` is upgraded to full — main gets no
#           import-closure guesses; a docs-only change still runs the docs tier
#   full    a tag push, or a push whose refs are unknown: always the full suite
# CI runs the whole suite on every PR and on main regardless.
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
# of the tree, and the tier it passed, so one identical tree pays the suite once.
# The stamp lives in the public gitdir on purpose: one tree, one digest, one stamp.
# It is found with `git rev-parse --absolute-git-dir`, because in a git worktree
# `.git` is a file and a hard-coded `.git/...` path could never be written — every
# worktree push used to re-run the whole suite inside the hook.
#
# ⚠ THE DIGEST MUST COVER EVERY INPUT THE SUITE READS, NOT JUST THE CODE.
# It used to be `find src tests scripts -name '*.py'` — Python sources only.
# That silently excluded almost everything the suite actually asserts against:
# pyproject.toml and uv.lock (dependency changes — the highest-risk class of
# all), collector/ (Go), ui/dashboard/ and packages/ (TypeScript), schemas/, the
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

_stamp_path() {
  local gitdir
  gitdir=$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE git rev-parse --absolute-git-dir 2>/dev/null) || return 1
  echo "$gitdir/nova-test-gate-passed"
}
# `--print-stamp-path` lets the guard prove the stamp is writable inside a git worktree.
if [ "${1:-}" = "--print-stamp-path" ]; then
  _stamp_path
  exit $?
fi

# Tier order; a stamp satisfies a push when it passed at least the tier now needed.
_rank() { case "$1" in none) echo 0 ;; docs) echo 1 ;; scoped) echo 2 ;; *) echo 3 ;; esac; }

REQUIRE="${NOVA_GATE_REQUIRE:-branch}"
case "$REQUIRE" in
  branch|main|full) ;;
  *) echo "test-gate: NOVA_GATE_REQUIRE='$REQUIRE' is not branch|main|full — treating it as full" >&2
     REQUIRE=full ;;
esac

DIGEST=$(_tree_files | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1)
STAMP=$(_stamp_path) || STAMP=""
STAMP_DIGEST=""
STAMP_TIER=""
if [ -n "$STAMP" ] && [ -f "$STAMP" ]; then
  read -r STAMP_DIGEST STAMP_TIER < "$STAMP"
  STAMP_TIER="${STAMP_TIER:-full}"   # a digest-only stamp predates tiers: it meant the full suite
fi
if [ "$STAMP_DIGEST" = "$DIGEST" ] && [ "$STAMP_TIER" = "full" ]; then
  echo "test-gate: already GREEN for this exact tree (stamp: full) — nothing to run" >&2
  exit 0
fi

PY=".venv/bin/python"
[ -x "$PY" ] || PY="uv run python"
PYTEST=".venv/bin/pytest"
[ -x "$PYTEST" ] || PYTEST="uv run pytest"

TIER=full
SELECTION=""
if [ "$REQUIRE" != "full" ]; then
  SEL_ERR=$(mktemp)
  if SELECTION=$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE \
                 $PY scripts/testsel.py --mode gate --require "$REQUIRE" 2>"$SEL_ERR"); then
    TIER=$(head -n1 <<<"$SELECTION")
    echo "test-gate: selector — $(cat "$SEL_ERR")" >&2
  else
    echo "test-gate: the selector failed — running the full suite: $(cat "$SEL_ERR")" >&2
    TIER=full
  fi
  rm -f "$SEL_ERR"
  case "$TIER" in none|docs|scoped|full) ;; *) TIER=full ;; esac
fi
# Belt and braces: the selector already applied --require; never trust a scoped
# answer for main even if it did not.
if [ "$REQUIRE" = "main" ] && [ "$TIER" = "scoped" ]; then
  TIER=full
fi

if [ "$STAMP_DIGEST" = "$DIGEST" ] && [ "$(_rank "$STAMP_TIER")" -ge "$(_rank "$TIER")" ]; then
  echo "test-gate: already GREEN for this exact tree (stamp: $STAMP_TIER, needed: $TIER) — nothing to run" >&2
  exit 0
fi

_stamp() {
  if [ -n "$STAMP" ] && echo "$DIGEST $TIER" > "$STAMP" 2>/dev/null; then
    echo "test-gate: GREEN ($TIER) — stamped; the next push of this tree skips the re-run" >&2
  else
    echo "test-gate: GREEN ($TIER) — but the stamp could not be written to '${STAMP:-?}'" >&2
  fi
}

if [ "$TIER" = "none" ]; then
  echo "test-gate: nothing differs from the merge base — no tests to run" >&2
  _stamp
  exit 0
fi

# Gate-width workers: the gate is user-initiated and its wall-clock is what the
# user waits on, so it gets a floor of half the cores even on a loaded box —
# `nice` keeps it polite on CPU, and the memory cap still applies because swap,
# not CPU, is what actually kills this machine. A background Stop-hook run gets
# no such floor.
export PYTEST_XDIST_AUTO_NUM_WORKERS="${PYTEST_XDIST_AUTO_NUM_WORKERS:-$(./scripts/test-workers.sh --gate)}"

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
CLEAN_ENV=(env -u FORCE_COLOR -u COLORTERM -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE)

if [ "$TIER" = "full" ]; then
  echo "test-gate: running make test-fast with $PYTEST_XDIST_AUTO_NUM_WORKERS workers" >&2
  if "${CLEAN_ENV[@]}" nice -n 10 make test-fast >&2; then
    _stamp
    exit 0
  fi
else
  mapfile -t FILES < <(tail -n +2 <<<"$SELECTION")
  echo "test-gate: running the $TIER tier — ${#FILES[@]} test file(s), $PYTEST_XDIST_AUTO_NUM_WORKERS workers" >&2
  # shellcheck disable=SC2086
  if "${CLEAN_ENV[@]}" nice -n 10 ./scripts/test-pool.sh $PYTEST -n auto --dist=loadgroup \
       --benchmark-disable -q -m "not container" "${FILES[@]}" >&2; then
    _stamp
    exit 0
  fi
fi

echo "test-gate: FAILED ($TIER) — the tree is red, nothing was stamped" >&2
exit 1
