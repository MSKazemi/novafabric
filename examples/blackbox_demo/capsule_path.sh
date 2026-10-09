# capsule_path.sh — sourced by run_demo.sh (POSIX sh, no bash-isms).
#
# capsule_path BASE_DIR CAPTURE_OUTPUT
#   Print the capsule directory that `nova capture --output-dir BASE_DIR`
#   wrote, given everything that command printed. Returns 1 (prints nothing)
#   when the output carries no run id or the directory does not exist.
#
# Why not grep the path itself: `nova capture` prints
#   "✓ Capsule written: <dir>  (run_id=<ULID>)"
# through a terminal-width console, which breaks a long <dir> across lines,
# and <dir> depends on NOVAFABRIC_CAPSULE_DIR / NOVAFABRIC_HOME. The run id is
# one short unbroken token, and BASE_DIR is passed explicitly, so
# BASE_DIR/<run id> is exact. tests/test_example_blackbox_demo.py runs this
# against real `nova capture` output.
capsule_path() {
    _cp_id=$(printf '%s\n' "$2" | grep -oE 'run_id=[0-9A-HJKMNP-TV-Z]{26}' | head -1 | cut -d= -f2)
    test -n "$_cp_id" || return 1
    test -d "$1/$_cp_id" || return 1
    printf '%s\n' "$1/$_cp_id"
}
