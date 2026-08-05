"""A help-output assertion that says what it actually saw.

Six `--help` assertions fail in CI and pass everywhere anyone has tried to
reproduce them (issue #21). Terminal width, no-TTY, `TERM=dumb`, `CI=true`,
random ordering, worker count and the Postgres tier have all been ruled out by
experiment.

**Root cause, found 2026-08-05 once the assertion started reporting what it
saw:** Rich emits ANSI escape sequences *inside* the option names when colour is
enabled, so a plain ``"--source" in result.output`` is False even though the flag
is plainly visible to a human reading the help. CI enables colour; pytest's
output capture makes Rich disable it locally, which is the entire reason this
failed in one place and not the other. Reproduce with ``FORCE_COLOR=1``:

    'much of the flag' in output          -> False
    'much of the flag' in strip_ansi(out) -> True

So the comparison is made against the *rendered text a user sees*, with escape
sequences removed. That is what these tests always meant to assert.

The failure report is kept — it is what turned an unreproducible mystery into a
one-line diagnosis, and it costs nothing while the tests pass.
"""

from __future__ import annotations

import re

__all__ = ["assert_flag_in_help", "strip_ansi"]

# CSI sequences (colour, bold, dim). Rich emits these mid-token.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Return *text* as a reader sees it, without terminal escape sequences."""
    return _ANSI.sub("", text)


def assert_flag_in_help(result: object, flag: str) -> None:
    """Assert *flag* appears in a Click/Typer ``result``'s help output.

    On failure, report the whole output rather than a fragment of it.
    """
    # Click/Typer results expose `.output`; `subprocess.CompletedProcess` exposes
    # `.stdout`. Both are used for help assertions in this suite, and reading only
    # one of them silently compares against an empty string — which looks exactly
    # like the bug this helper exists to diagnose.
    raw = getattr(result, "output", None)
    if raw is None:
        raw = getattr(result, "stdout", "") or ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    output = strip_ansi(raw)

    if flag in output:
        return

    lines = output.splitlines()
    widest = max((len(line) for line in lines), default=0)

    # A wrapped flag is the leading hypothesis, so test it explicitly: does the
    # flag appear if whitespace (including newlines) is collapsed away?
    squashed = re.sub(r"\s+", "", output)
    wrapped = re.sub(r"\s+", "", flag) in squashed

    detail = [
        f"{flag!r} is not in the help output.",
        f"  rendered width : {widest} columns",
        f"  output lines   : {len(lines)}",
        f"  present if whitespace is ignored: {wrapped}"
        + ("   <-- it is WRAPPED, not missing" if wrapped else ""),
        f"  ANSI sequences stripped: {len(raw) - len(output)} chars",
        "",
        "--- full output ---",
        output or "(empty)",
        "--- end output ---",
        "",
        f"repr (first 2000 chars): {output[:2000]!r}",
    ]
    raise AssertionError("\n".join(detail))
