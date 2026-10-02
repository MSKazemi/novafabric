"""``capsule_holds`` must not fail open on a legal-hold check.

Holds are registry-global at ``<capsule base>/../registries/*/holds.jsonl``.
The function was documented to take the capsule *base*; handed a per-run
directory it looked under ``<base>/registries`` (which does not exist) and
returned ``()`` -- "no holds" -- for a held capsule.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.evidence.cart import HoldLookupError, capsule_holds


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """<root>/capsules/<run>/capsule.yaml + <root>/registries/default/holds.jsonl."""
    base = tmp_path / "capsules"
    run = base / "run-1"
    run.mkdir(parents=True)
    (run / "capsule.yaml").write_text("run_id: run-1\n")
    reg = tmp_path / "registries" / "default"
    reg.mkdir(parents=True)
    (reg / "holds.jsonl").write_text(
        json.dumps({"hold_id": "H-ACTIVE", "released_at": None})
        + "\n"
        + json.dumps({"hold_id": "H-OLD", "released_at": "2026-01-01T00:00:00Z"})
        + "\n"
    )
    return base, run


def test_base_directory_reports_the_active_hold(tmp_path: Path) -> None:
    base, _ = _layout(tmp_path)
    assert capsule_holds(base) == ("H-ACTIVE",)


def test_per_run_directory_resolves_to_its_base_and_reports_the_hold(tmp_path: Path) -> None:
    _, run = _layout(tmp_path)
    assert capsule_holds(run) == ("H-ACTIVE",), "a per-run dir must not read as 'no holds'"


def test_unresolvable_directory_raises_rather_than_reporting_no_holds(tmp_path: Path) -> None:
    with pytest.raises(HoldLookupError):
        capsule_holds(tmp_path / "does-not-exist")


def test_a_file_path_is_unresolvable(tmp_path: Path) -> None:
    f = tmp_path / "x"
    f.write_text("")
    with pytest.raises(HoldLookupError):
        capsule_holds(f)


def test_a_base_with_no_registries_has_no_holds(tmp_path: Path) -> None:
    """Fresh install: no registry was ever created, so nothing can be held."""
    base = tmp_path / "capsules"
    base.mkdir()
    assert capsule_holds(base) == ()


def test_a_corrupt_hold_line_still_blocks_through_the_per_run_form(tmp_path: Path) -> None:
    _, run = _layout(tmp_path)
    (tmp_path / "registries" / "default" / "holds.jsonl").write_text("{not json\n")
    assert capsule_holds(run) == ("__corrupt__:default",)
