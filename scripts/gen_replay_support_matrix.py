#!/usr/bin/env python3
"""Render the mocked-replay support matrix into docs/architecture/replay-modes.md.

Usage:
    python scripts/gen_replay_support_matrix.py           # rewrite the generated block
    python scripts/gen_replay_support_matrix.py --check   # exit 1 if the block is stale

The rows live in ``src/novafabric/replay/_support_matrix.py`` (ADR-0304); only the
text between the generated-block markers is rewritten. Deterministic: same rows in,
byte-identical page out. ``tests/replay/test_support_matrix_is_generated.py`` fails
on drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "docs" / "architecture" / "replay-modes.md"


def updated_page(text: str) -> str:
    from novafabric.replay._support_matrix import BEGIN_MARKER, END_MARKER, render_block

    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER) + len(END_MARKER)
    return text[:start] + render_block() + text[end:]


def main(argv: list[str]) -> int:
    current = PAGE.read_text(encoding="utf-8")
    fresh = updated_page(current)
    if "--check" in argv:
        if fresh != current:
            print(f"{PAGE.relative_to(REPO)} is stale: run scripts/gen_replay_support_matrix.py")
            return 1
        return 0
    if fresh != current:
        PAGE.write_text(fresh, encoding="utf-8")
        print(f"wrote {PAGE.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
