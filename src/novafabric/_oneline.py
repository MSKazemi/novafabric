"""Make an untrusted value safe to print inside one line of a text report.

Record ids and file paths come from capsule files, which a run (or whoever edited the
capsule) controls. Printed raw, a newline lets an id forge a line of the report (a fake
"No differences found."), an ESC starts a terminal control sequence, and a very long or
nested value floods the output. JSON output is unaffected: it carries the raw value.
"""

from __future__ import annotations

import unicodedata
from typing import Any

DEFAULT_LIMIT = 80
_ELLIPSIS = "…"
#: Control, format (bidi overrides, zero-width), surrogate (cannot be encoded), private-use,
#: unassigned, and line/paragraph separators.
_REPLACED = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def one_line(value: Any, limit: int = DEFAULT_LIMIT) -> str:
    """``str(value)`` on one line: non-printable characters become ``?``, capped at ``limit``."""
    text = str(value)[: limit + 1]  # bound the work done on a huge value
    text = "".join(
        "?" if unicodedata.category(ch) in _REPLACED else ch for ch in text
    )
    return text if len(text) <= limit else text[: limit - 1] + _ELLIPSIS
