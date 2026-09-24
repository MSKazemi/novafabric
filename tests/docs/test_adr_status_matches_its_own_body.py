"""An ADR that documents shipped code must not declare itself `proposed`.

`docs/decisions.md` — the **public** decisions index a visitor reads — is
generated from ADR frontmatter. On 2026-09-06 ten ADRs (0240–0249) still read
`status: proposed` after their first slices shipped in the tagged release
v0.101.0, and seven of them already carried an *Implementation status* section
describing the shipped code. The index therefore told every visitor that ten
shipped features were merely proposed.

CLAUDE.md warns that this repo has *"repeatedly found docs both overclaiming and
underclaiming"*. This guard covers the underclaiming direction, which is the
easier one to miss: nothing breaks, no user complains, and the project simply
gets less credit than it has earned while its own index contradicts its release
notes.

⚠ `design/` is private, so this test skips cleanly in a public checkout rather
than failing on an absent directory.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ADR_DIR = REPO / "design" / "adr"

pytestmark = pytest.mark.skipif(
    not ADR_DIR.is_dir(), reason="design/ is private and absent from this checkout"
)

_STATUS_RE = re.compile(r"^status:\s*(\S+)\s*$", re.M)


def _frontmatter(text: str) -> str:
    """The YAML frontmatter block, or "" when the file has none.

    ⚠ Scanning the whole document is wrong and this guard did it at first: it
    matched `status: success` inside a **capsule manifest example** in ADR-0256,
    which has no frontmatter at all. A guard that fires on legitimate example
    content is worse than no guard — it trains the reader to ignore it, the same
    failure ADR-0230's read-only sweep had to avoid.
    """
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def _declared_status(text: str) -> str | None:
    match = _STATUS_RE.search(_frontmatter(text))
    return match.group(1) if match else None

#: Headings that mean "this describes code that exists". A body carrying one of
#: these while the frontmatter says `proposed` is the document disagreeing with
#: itself.
_SHIPPED_MARKERS = ("## Implementation status",)


def _adrs() -> list[Path]:
    return sorted(ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))


def test_there_are_adrs_to_check() -> None:
    """Non-vacuity: a glob that matches nothing passes every test below."""
    assert len(_adrs()) > 100, "the ADR sweep found suspiciously few files"


def test_no_adr_documents_shipped_code_while_calling_itself_proposed() -> None:
    offenders: list[str] = []
    for path in _adrs():
        text = path.read_text(encoding="utf-8")
        if _declared_status(text) != "proposed":
            continue
        for marker in _SHIPPED_MARKERS:
            if marker in text:
                offenders.append(f"{path.name}: has '{marker}' but status: proposed")
                break

    assert not offenders, (
        f"{len(offenders)} ADR(s) describe shipped code while declaring themselves "
        "proposed. `docs/decisions.md` is generated from this frontmatter, so the "
        "public index under-claims them:\n" + "\n".join(offenders)
    )


def test_a_declared_status_is_a_recognised_one() -> None:
    """A typo'd status silently drops an ADR out of every status-based sweep.

    ⓘ Scoped to ADRs that *have* frontmatter with a status field. Many do not — a long-standing
    pattern in the older ADRs, and `gen_decisions_index.py` already handles their
    absence. Demanding one on every file would be a 108-file edit unrelated to the
    defect this module exists for, so it is recorded here rather than enforced.
    """
    allowed = {"proposed", "accepted", "rejected", "superseded", "deprecated", "draft"}
    bad: list[str] = []
    for path in _adrs():
        status = _declared_status(path.read_text(encoding="utf-8"))
        if status is not None and status not in allowed:
            bad.append(f"{path.name}: unrecognised status {status!r}")
    assert not bad, "\n".join(bad)


def test_some_adrs_do_declare_a_status() -> None:
    """Non-vacuity for the test above: if none did, it would pass while blind."""
    with_status = [p for p in _adrs() if _declared_status(p.read_text(encoding="utf-8"))]
    assert len(with_status) > 50, f"only {len(with_status)} ADRs carry a status field"
