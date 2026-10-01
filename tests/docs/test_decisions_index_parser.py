"""``scripts/gen_decisions_index.py`` reads every ADR header generation.

Regression (2026-10-01): ADRs 0256–0268 use a bullet-list header —
``- **Status:** Accepted`` / ``- **Date:** …`` under ``# ADR 0256 — Title``
(space, not hyphen). The body-status regex accepted only ``**Status:**`` and
``- Status:``, so the **public** index rendered all thirteen as ``unknown`` and
fell back to slug titles. These tests run against synthetic ADRs in a temp dir,
so they pass in a public clone where ``design/`` is absent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_decisions_index.py"


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_decisions_index", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FRONTMATTER = """---
id: adr-0250
title: "Introspect frameworks by shape"
type: adr
status: accepted
created_at: 2026-08-08
---

# ADR-0250 — Introspect frameworks by shape
"""

BOLD_BODY = """<!-- old header -->
# ADR-0010 — External resources

**Status:** Accepted 2026-05-01 — first slice
**Date:** 2026-05-01
"""

DASH_PLAIN = """# ADR-0011 — Plain dash status

- Status: Proposed
"""

BULLET_BOLD = """# ADR 0256 — PROV-JSON export must include the run's own provenance

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Mohsen Seyedkazemi Ardebili
"""

BULLET_BOLD_PROPOSED = """# ADR 0268 — Merkle log verification: the 200 ms target costs a guarantee

- **Status:** Proposed
- **Date:** 2026-09-04
"""

INDEX_TITLE = """---
id: adr-0273
title: "Full internal title with reasoning"
index_title: "Public index title"
type: adr
status: proposed
created_at: 2026-09-24
---
"""


def _write(adr_dir: Path, name: str, text: str) -> None:
    (adr_dir / name).write_text(text, encoding="utf-8")


@pytest.fixture
def adr_dir(tmp_path: Path) -> Path:
    d = tmp_path / "adr"
    d.mkdir()
    _write(d, "0010-external-resources.md", BOLD_BODY)
    _write(d, "0011-plain-dash.md", DASH_PLAIN)
    _write(d, "0250-framework-introspection.md", FRONTMATTER)
    _write(d, "0256-prov-json-exports-the-run-itself.md", BULLET_BOLD)
    _write(d, "0268-merkle-verification-cost.md", BULLET_BOLD_PROPOSED)
    _write(d, "0273-commercial-model.md", INDEX_TITLE)
    _write(d, "README.md", "# not an ADR\n")
    _write(d, "STATUS-2026-10-01.md", "**Status:** not an ADR\n")
    return d


def _by_number(gen: ModuleType, adr_dir: Path) -> dict[str, object]:
    return {adr.number: adr for adr in gen.collect(adr_dir)}


def test_bullet_bold_header_is_parsed(gen: ModuleType, adr_dir: Path) -> None:
    adrs = _by_number(gen, adr_dir)
    adr = adrs["0256"]
    assert adr.status == "accepted"  # type: ignore[attr-defined]
    assert adr.created_at == "2026-08-28"  # type: ignore[attr-defined]
    assert adr.title == "PROV-JSON export must include the run's own provenance"  # type: ignore[attr-defined]
    assert adrs["0268"].status == "proposed"  # type: ignore[attr-defined]


def test_older_generations_still_parse(gen: ModuleType, adr_dir: Path) -> None:
    adrs = _by_number(gen, adr_dir)
    assert adrs["0010"].status == "accepted"  # type: ignore[attr-defined]
    assert adrs["0010"].created_at == "2026-05-01"  # type: ignore[attr-defined]
    assert adrs["0010"].title == "External resources"  # type: ignore[attr-defined]
    assert adrs["0011"].status == "proposed"  # type: ignore[attr-defined]
    assert adrs["0250"].status == "accepted"  # type: ignore[attr-defined]
    assert adrs["0250"].title == "Introspect frameworks by shape"  # type: ignore[attr-defined]


def test_no_adr_renders_unknown(gen: ModuleType, adr_dir: Path) -> None:
    assert [a.number for a in gen.collect(adr_dir) if a.status == "unknown"] == []


def test_non_adr_files_are_skipped(gen: ModuleType, adr_dir: Path) -> None:
    assert sorted(_by_number(gen, adr_dir)) == ["0010", "0011", "0250", "0256", "0268", "0273"]


def test_index_title_overrides_title(gen: ModuleType, adr_dir: Path) -> None:
    adr = _by_number(gen, adr_dir)["0273"]
    assert adr.title == "Public index title"  # type: ignore[attr-defined]
    rendered = gen.render(gen.collect(adr_dir))
    assert "Full internal title" not in rendered


def test_adr_dir_flag_writes_and_checks(
    gen: ModuleType, adr_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "decisions.md"
    monkeypatch.setattr(gen, "OUTPUT", out)
    monkeypatch.setattr(gen, "REPO_ROOT", tmp_path)
    assert gen.main(["--adr-dir", str(adr_dir)]) == 0
    assert "| `ADR-0256` | PROV-JSON export" in out.read_text(encoding="utf-8")
    assert gen.main(["--adr-dir", str(adr_dir), "--check"]) == 0
    _write(adr_dir, "0274-new.md", BULLET_BOLD.replace("0256", "0274"))
    assert gen.main(["--adr-dir", str(adr_dir), "--check"]) == 1


def test_missing_adr_dir_is_a_clean_skip(gen: ModuleType, tmp_path: Path) -> None:
    assert gen.main(["--adr-dir", str(tmp_path / "absent")]) == 0
