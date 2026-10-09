"""Machine-readable CLI output that stays machine-readable.

Every ``--json`` / ``--output-format json`` path writes through :func:`emit_json`,
never through a Rich console. ``Console.print_json`` syntax-highlights its output
whenever colour is forced (``FORCE_COLOR``, or Typer/Rich detecting GitHub
Actions), so a CI step or a ``> out.json`` redirect received ANSI escapes where
JSON was promised; ``Console.print`` of a JSON string additionally wraps long
lines at the terminal width and treats ``[...]`` as markup. A guard test
(``tests/cli/test_json_output_is_plain.py``) keeps both out of ``novafabric.cli``.
"""

from __future__ import annotations

import json

import typer

__all__ = ["emit_json"]


def emit_json(document: str, *, indent: int = 2) -> None:
    """Write the JSON *document* to stdout: no colour, no wrapping, no markup.

    *document* is re-serialised exactly as ``rich.console.Console.print_json``
    did (``indent=2``, ``ensure_ascii=False``, key order preserved), so the bytes
    match what the commands emitted before, minus any ANSI escapes.

    Raises:
        json.JSONDecodeError: *document* is not valid JSON — a programming error.
    """
    typer.echo(json.dumps(json.loads(document), indent=indent, ensure_ascii=False))
