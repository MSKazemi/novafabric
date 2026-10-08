"""Execute a notebook's code cells in order, as one plain Python process.

The no-Jupyter path for this example. `nbconvert` needs Jupyter and a kernel;
this needs only the standard library, so the same analysis can be captured on a
machine (or a CI runner) that has neither:

    nova capture -- python3 run_cells.py analysis.ipynb

It is deliberately small and deliberately limited: every code cell runs in one
shared namespace, top to bottom, exactly as "Restart & Run All" would — but
there is no kernel, so IPython syntax (`%magic`, `!shell`) is refused rather
than half-supported. Use nbconvert for a notebook that needs those.

One difference from the nbconvert path matters for evidence: here a cell's
`print()` goes to this process's stdout, so it DOES land in the capsule's
`outputs/stdout.txt`. Under nbconvert the kernel routes it into the notebook
document instead, and it reaches no capsule file at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


class NotebookError(Exception):
    """The file is not a notebook this runner can execute."""


def code_cells(notebook: Path) -> list[str]:
    """Return the source of every code cell, in document order."""
    try:
        doc = json.loads(notebook.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotebookError(f"{notebook}: not a readable notebook ({exc})") from exc
    if doc.get("nbformat") != 4:
        raise NotebookError(f"{notebook}: only nbformat 4 is supported")
    sources: list[str] = []
    for index, cell in enumerate(doc.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) else str(source)
        for line in text.splitlines():
            if line.lstrip().startswith(("%", "!")):
                raise NotebookError(
                    f"{notebook}: cell {index} uses IPython syntax ({line.strip()!r}); "
                    "run it with nbconvert instead"
                )
        sources.append(text)
    return sources


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} NOTEBOOK.ipynb", file=sys.stderr)
        return 2
    notebook = Path(argv[1])
    try:
        cells = code_cells(notebook)
    except NotebookError as exc:
        print(f"run_cells: {exc}", file=sys.stderr)
        return 2
    namespace: dict[str, object] = {"__name__": "__main__"}
    for index, source in enumerate(cells):
        exec(compile(source, f"{notebook}#code-cell-{index}", "exec"), namespace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
