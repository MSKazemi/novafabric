"""``NOVA_CAP003_ENABLED`` defaults to **false** on every surface (ADR-0069 / ADR-0062).

ADR-0069 claimed the default "becomes true"; the code says false, and false is
correct: SCALE-ADR-003 / ADR-0066 make it a mandatory safety gate until EU-GDPR
legal counsel reviews cap-003's own OQ-01, and ADR-0069 resolves a *different*
capability's OQ-01 (cap-001 DEK crypto-shredding). The ADR text was corrected;
these tests pin the code side so the two cannot drift again:

- the shared parser ``cap003_enabled()`` and its declared default;
- the writer (``DualObjectStore``) and the CLI reporter (``nova storage inspect``);
- a source scan: any module that still reads the variable directly must use the
  ``"false"`` default (``serve/app.py`` reads it inline today).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app as cli_app
from novafabric.storage.dual_object_store import (
    CAP003_DEFAULT,
    CAP003_ENV,
    DualObjectStore,
    cap003_enabled,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "novafabric"


@pytest.fixture(autouse=True)
def _unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CAP003_ENV, raising=False)


def test_declared_default_is_false() -> None:
    assert CAP003_ENV == "NOVA_CAP003_ENABLED"
    assert CAP003_DEFAULT is False


def test_unset_means_disabled() -> None:
    assert cap003_enabled() is False
    assert DualObjectStore().enabled is False


@pytest.mark.parametrize("value", ["", "false", "0", "yes", "1", "on", "nope"])
def test_only_literal_true_enables(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(CAP003_ENV, value)
    assert cap003_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "True"])
def test_explicit_true_enables(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(CAP003_ENV, value)
    assert cap003_enabled() is True
    assert DualObjectStore().enabled is True


def test_cli_reports_disabled_by_default() -> None:
    result = CliRunner().invoke(
        cli_app, ["storage", "inspect", "--run-id", "run-cap003-default", "--json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["cap003_enabled"] is False
    assert body["pii_object_key"] is None


_DIRECT_READ = re.compile(
    r"""(?:os\.getenv|os\.environ\.get)\(\s*["']NOVA_CAP003_ENABLED["']\s*(?:,\s*([^)]*))?\)"""
)


def test_every_direct_reader_defaults_to_false() -> None:
    offenders: list[str] = []
    readers = 0
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in _DIRECT_READ.finditer(text):
            readers += 1
            default = (match.group(1) or "").strip()
            if default not in {'"false"', "'false'"}:
                offenders.append(f"{path.relative_to(SRC)}: {match.group(0)}")
    assert readers >= 1  # serve/app.py still reads it inline; the scan must see it
    assert offenders == [], offenders
