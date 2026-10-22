"""Unit tests for the one legal-hold store (``novafabric.retention.holds``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.audit import AuditLog
from novafabric.retention import holds as hs


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(audit))
    return {
        "canonical": tmp_path / "home" / "registries",
        "cli": cwd / ".novafabric" / "registries",
        "serve": tmp_path / "data" / "registries",
        "audit": audit,
    }


def _write(root: Path, registry: str, *lines: str) -> Path:
    path = root / registry / hs.HOLDS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines))
    return path


def _rec(hold_id: str, released_at: str | None = None) -> str:
    return json.dumps({
        "hold_id": hold_id, "registry": "r", "reason": "x", "duration_days": None,
        "created_at": "2026-01-01T00:00:00+00:00", "released_at": released_at,
    })


def test_place_writes_canonical_ledger_and_audits(roots: dict[str, Path]) -> None:
    record = hs.place_hold("r", "exam", 30, actor="tester")
    stored = json.loads((roots["canonical"] / "r" / hs.HOLDS_FILENAME).read_text())
    assert stored == record.as_dict()
    assert stored["duration_days"] == 30 and stored["released_at"] is None
    entries = AuditLog(roots["audit"]).query(resource_id="r")
    assert [str(e.event_type.value) for e in entries] == ["hold.create"]
    assert entries[0].actor == "tester"
    assert entries[0].details["hold_id"] == record.hold_id


def test_hold_roots_are_deduplicated(roots: dict[str, Path]) -> None:
    listed = hs.hold_roots([roots["canonical"], roots["serve"]])
    assert listed == [roots["canonical"], roots["cli"], roots["serve"]]


@pytest.mark.parametrize(
    "bad", ["", "  ", "../x", "a/b", "a\\b", ".", "..", " lead", "x" * 256, "a\x00b"]
)
def test_invalid_registry_names_refused(roots: dict[str, Path], bad: str) -> None:
    with pytest.raises(hs.InvalidRegistryNameError):
        hs.place_hold(bad, "reason", actor="t")
    with pytest.raises(hs.InvalidRegistryNameError):
        hs.active_hold_ids(bad)


def test_place_validates_reason_and_duration(roots: dict[str, Path]) -> None:
    with pytest.raises(ValueError, match="reason"):
        hs.place_hold("r", "   ", actor="t")
    with pytest.raises(ValueError, match="duration_days"):
        hs.place_hold("r", "ok", -1, actor="t")


def test_union_across_roots_and_registry_filter(roots: dict[str, Path]) -> None:
    _write(roots["canonical"], "r", _rec("hold-a"))
    _write(roots["cli"], "r", _rec("hold-b"), _rec("hold-gone", "2026-02-01"))
    _write(roots["serve"], "r", _rec("hold-c"))
    _write(roots["serve"], "other", _rec("hold-o"))
    assert hs.active_hold_ids("r") == ["hold-a", "hold-b"]
    assert hs.active_hold_ids("r", extra_roots=[roots["serve"]]) == [
        "hold-a", "hold-b", "hold-c",
    ]
    assert sorted(hs.active_hold_ids(extra_roots=[roots["serve"]])) == [
        "hold-a", "hold-b", "hold-c", "hold-o",
    ]
    released = [h for h in hs.read_holds("r") if not h.active]
    assert [h.hold_id for h in released] == ["hold-gone"]


def test_a_copy_still_active_anywhere_wins(roots: dict[str, Path]) -> None:
    _write(roots["canonical"], "r", _rec("hold-dup", "2026-02-01"))
    _write(roots["cli"], "r", _rec("hold-dup"))
    assert hs.active_hold_ids("r") == ["hold-dup"]


@pytest.mark.parametrize(
    "line", ["{not json", "[1, 2]", json.dumps({"reason": "no id"}), json.dumps({"hold_id": 7})]
)
def test_damaged_lines_read_as_active_holds(roots: dict[str, Path], line: str) -> None:
    _write(roots["canonical"], "r", line)
    held = hs.active_holds("r")
    assert [h.hold_id for h in held] == ["__corrupt__:r"]
    assert held[0].corrupt


def test_unreadable_ledger_reads_as_active_hold(roots: dict[str, Path]) -> None:
    path = _write(roots["canonical"], "r", _rec("hold-a"))
    path.write_bytes(b"\xff\xfe\x00bad")
    assert hs.active_hold_ids("r") == ["__corrupt__:r"]


def test_release_stamps_every_active_copy_and_keeps_damaged_lines(
    roots: dict[str, Path],
) -> None:
    a = _write(roots["canonical"], "r", "{not json", _rec("hold-x"), _rec("hold-y"))
    b = _write(roots["serve"], "r2", _rec("hold-x"))
    registries = hs.release_hold("hold-x", actor="t", extra_roots=[roots["serve"]])
    assert registries == ["r", "r2"]
    lines = a.read_text().splitlines()
    assert lines[0] == "{not json"  # preserved verbatim: still fails closed
    assert json.loads(lines[1])["released_at"] is not None
    assert json.loads(lines[2])["released_at"] is None
    assert json.loads(b.read_text())["released_at"] is not None
    assert hs.active_hold_ids(extra_roots=[roots["serve"]]) == ["__corrupt__:r", "hold-y"]


def test_release_unknown_or_already_released(roots: dict[str, Path]) -> None:
    _write(roots["canonical"], "r", _rec("hold-done", "2026-02-01"))
    with pytest.raises(hs.HoldNotFoundError):
        hs.release_hold("hold-done", actor="t")
    with pytest.raises(hs.HoldNotFoundError):
        hs.release_hold("hold-missing", actor="t")


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a\\b"])
def test_release_refuses_path_like_ids(roots: dict[str, Path], bad: str) -> None:
    with pytest.raises(hs.InvalidRegistryNameError):
        hs.release_hold(bad, actor="t")
