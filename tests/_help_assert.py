"""A help-output assertion that says what it actually saw.

Six `--help` assertions fail in CI and pass everywhere anyone has tried to
reproduce them (issue #21). Terminal width, no-TTY, `TERM=dumb`, `CI=true`,
random ordering, worker count and the Postgres tier have all been ruled out by
experiment.

The investigation stalled on a diagnostic problem rather than a hypothesis
problem: **pytest elides long assertion messages with `...`**, so the CI log
shows the top and bottom of a rendered Rich help box and nothing in between. It
is genuinely unknown whether the flag is absent or merely wrapped across a line.

`assert_flag_in_help` removes that ambiguity. When it fails it prints the full
rendered output, its width, and whether the flag appears with internal
whitespace — enough that the next failing CI run explains itself instead of
prompting another round of guessing.
"""

from __future__ import annotations

import re

__all__ = ["assert_flag_in_help"]


def assert_flag_in_help(result: object, flag: str) -> None:
    """Assert *flag* appears in a Click/Typer ``result``'s help output.

    On failure, report the whole output rather than a fragment of it.
    """
    output: str = getattr(result, "output", "")

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
        "",
        "--- full output ---",
        output or "(empty)",
        "--- end output ---",
        "",
        f"repr (first 2000 chars): {output[:2000]!r}",
    ]
    raise AssertionError("\n".join(detail))
