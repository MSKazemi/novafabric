"""nova a2a-objects — CLI surface for ADR-0149 D1 / NF-172."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

DOC = {
    "tasks": [{"id": "task-1", "state": "completed", "sessionId": "s-9"}],
    "messages": [{"messageId": "msg-1", "role": "agent",
                  "parts": [{"kind": "text", "text": "secret"}]}],
    "artifacts": [{"name": "r.pdf", "content_hash": "sha256:" + "c" * 64}],
}


@pytest.fixture()
def objects_file(tmp_path: Path) -> Path:
    p = tmp_path / "objects.json"
    p.write_text(json.dumps(DOC))
    return p


def _map(objects: Path, out: Path):
    return runner.invoke(app, ["a2a-objects", "map", "--objects", str(objects),
                               "--out", str(out)])


def test_map_writes_a_facet_with_a_manifest_and_digest(
    objects_file: Path, tmp_path: Path
) -> None:
    out = tmp_path / "facet.json"
    result = _map(objects_file, out)

    assert result.exit_code == 0, result.output
    facet = json.loads(out.read_text())
    assert facet["roundtrip_digest"].startswith("sha256:")
    assert facet["mapping_manifest"]
    assert facet["tasks"][0]["task_id"] == "task-1"


def test_message_content_never_reaches_the_facet(
    objects_file: Path, tmp_path: Path
) -> None:
    out = tmp_path / "facet.json"
    _map(objects_file, out)
    assert "secret" not in out.read_text()


def test_map_reports_fields_it_does_not_carry(
    objects_file: Path, tmp_path: Path
) -> None:
    result = _map(objects_file, tmp_path / "facet.json")
    assert "not carried" in result.output or "unmapped" in result.output


def test_roundtrip_passes_on_an_untouched_facet(
    objects_file: Path, tmp_path: Path
) -> None:
    facet = tmp_path / "facet.json"
    assert _map(objects_file, facet).exit_code == 0

    result = runner.invoke(app, ["a2a-objects", "roundtrip", "--facet", str(facet)])
    assert result.exit_code == 0, result.output


def test_roundtrip_exits_one_and_names_the_object_when_tampered(
    objects_file: Path, tmp_path: Path
) -> None:
    facet_path = tmp_path / "facet.json"
    assert _map(objects_file, facet_path).exit_code == 0

    doc = json.loads(facet_path.read_text())
    doc["tasks"][0]["lifecycle_state"] = "failed"
    facet_path.write_text(json.dumps(doc))

    result = runner.invoke(app, ["a2a-objects", "roundtrip",
                                 "--facet", str(facet_path)])
    assert result.exit_code == 1, result.output
    assert "task" in result.output


def test_an_empty_document_writes_nothing_and_exits_zero(tmp_path: Path) -> None:
    p = tmp_path / "empty.json"
    p.write_text("{}")
    result = runner.invoke(app, ["a2a-objects", "map", "--objects", str(p)])
    assert result.exit_code == 0


def test_a_malformed_object_exits_two(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"tasks": [{"state": "done"}]}))
    result = runner.invoke(app, ["a2a-objects", "map", "--objects", str(p)])
    assert result.exit_code == 2


def test_a_missing_file_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["a2a-objects", "map", "--objects",
                                 str(tmp_path / "nope.json")])
    assert result.exit_code == 2
