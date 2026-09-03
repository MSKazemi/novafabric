"""One error contract for the evidence CLIs added in ADR-0147 and ADR-0149.

Every one of these commands reads a JSON document and writes a facet. Their error
paths were the least-exercised code in the new surface, and this repository's own
record is that *24 of 24 defects found in two Azure campaigns were in
never-executed paths*. So these tests walk the failure branches deliberately:
unreadable input, valid JSON that is not an object, and a structurally invalid
facet handed to a verifier.

They are written as a table rather than per-command so that the contract itself is
the assertion — a fifth command that exits 0 on garbage, or reports the failure on
stdout, fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

#: (command, the option that takes a document) for every reader in the new surface.
DOCUMENT_READERS = [
    (["a2a-card", "capture"], "--card"),
    (["a2a-card", "verify"], "--facet"),
    (["a2a-objects", "map"], "--objects"),
    (["a2a-objects", "roundtrip"], "--facet"),
    (["assure-run", "check"], "--attestation"),
]


def _extra(cmd: list[str]) -> list[str]:
    """Options a command needs beyond its document, to reach the parse step."""
    if cmd == ["assure-run", "check"]:
        return ["--now", "2026-07-12T00:00:00Z"]
    return []


@pytest.mark.parametrize(("cmd", "opt"), DOCUMENT_READERS,
                         ids=[" ".join(c) for c, _ in DOCUMENT_READERS])
def test_unparseable_json_exits_two(cmd: list[str], opt: str, tmp_path: Path) -> None:
    doc = tmp_path / "bad.json"
    doc.write_text("{not json at all")

    result = runner.invoke(app, [*cmd, opt, str(doc), *_extra(cmd)])

    assert result.exit_code == 2, f"{cmd}: {result.output}"


@pytest.mark.parametrize(("cmd", "opt"), DOCUMENT_READERS,
                         ids=[" ".join(c) for c, _ in DOCUMENT_READERS])
def test_valid_json_that_is_not_an_object_exits_two(
    cmd: list[str], opt: str, tmp_path: Path
) -> None:
    """A JSON array parses fine and is still not a facet."""
    doc = tmp_path / "arr.json"
    doc.write_text("[1, 2, 3]")

    result = runner.invoke(app, [*cmd, opt, str(doc), *_extra(cmd)])

    assert result.exit_code == 2, f"{cmd}: {result.output}"


@pytest.mark.parametrize(("cmd", "opt"), DOCUMENT_READERS,
                         ids=[" ".join(c) for c, _ in DOCUMENT_READERS])
def test_a_missing_file_exits_two(cmd: list[str], opt: str, tmp_path: Path) -> None:
    result = runner.invoke(app, [*cmd, opt, str(tmp_path / "nope.json"),
                                 *_extra(cmd)])

    assert result.exit_code == 2, f"{cmd}: {result.output}"


# ── structurally invalid facets reach the verifier and are refused ───────────


@pytest.mark.parametrize(
    ("cmd", "opt", "payload"),
    [
        (["a2a-card", "verify"], "--facet", {"card": {}}),
        (["a2a-objects", "roundtrip"], "--facet", {"tasks": "not-a-list"}),
        (["assure-run", "check"], "--attestation", {"schedule_id": "s"}),
    ],
    ids=["a2a-card", "a2a-objects", "assure-run"],
)
def test_a_structurally_invalid_facet_exits_two(
    cmd: list[str], opt: str, payload: dict, tmp_path: Path
) -> None:
    doc = tmp_path / "facet.json"
    doc.write_text(json.dumps(payload))

    result = runner.invoke(app, [*cmd, opt, str(doc), *_extra(cmd)])

    assert result.exit_code == 2, f"{cmd}: {result.output}"


def test_assure_baseline_verify_refuses_a_missing_capsule(tmp_path: Path) -> None:
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps({"baseline_id": "bl", "criterion": "goal",
                               "pinned_at": "2026-07-01T00:00:00Z",
                               "runs": [{"run_id": "r",
                                         "baseline_root": "sha256:" + "a" * 64}]}))

    result = runner.invoke(app, ["assure-baseline", "verify", "--pin", str(pin),
                                 "--capsule", str(tmp_path / "nope"), "--run", "r"])

    assert result.exit_code == 2, result.output


def test_assure_baseline_verify_refuses_an_invalid_pin(tmp_path: Path) -> None:
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps({"baseline_id": "bl"}))
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: r\n")

    result = runner.invoke(app, ["assure-baseline", "verify", "--pin", str(pin),
                                 "--capsule", str(capsule), "--run", "r"])

    assert result.exit_code == 2, result.output


# ── stdout mode: every command must work without --out ──────────────────────


def test_capture_and_map_print_to_stdout_without_out(tmp_path: Path) -> None:
    """`--out` was supplied in every earlier test, so this branch was unexercised."""
    card = tmp_path / "card.json"
    card.write_text(json.dumps({"name": "planner", "skills": []}))
    objects = tmp_path / "objects.json"
    objects.write_text(json.dumps({"tasks": [{"id": "t", "state": "working"}]}))

    card_result = runner.invoke(app, ["a2a-card", "capture", "--card", str(card)])
    assert card_result.exit_code == 0, card_result.output
    assert "card_fingerprint" in card_result.output

    map_result = runner.invoke(app, ["a2a-objects", "map", "--objects", str(objects)])
    assert map_result.exit_code == 0, map_result.output
    assert "roundtrip_digest" in map_result.output


def test_assure_commands_print_to_stdout_without_out(tmp_path: Path) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: r\n")

    pin_result = runner.invoke(app, [
        "assure-baseline", "pin", "--capsule", str(capsule), "--run", "r",
        "--id", "bl", "--criterion", "goal", "--pinned-at", "2026-07-01T00:00:00Z",
    ])
    assert pin_result.exit_code == 0, pin_result.output
    assert "baseline_id" in pin_result.output

    run_result = runner.invoke(app, [
        "assure-run", "record", "--schedule", "s",
        "--ran-at", "2026-07-12T00:00:00Z", "--cadence", "3600",
    ])
    assert run_result.exit_code == 0, run_result.output
    assert "next_due" in run_result.output


def test_a_card_with_missing_fields_reports_them(tmp_path: Path) -> None:
    """The `missing_fields` report branch was never reached."""
    card = tmp_path / "card.json"
    card.write_text(json.dumps({"name": "planner"}))

    result = runner.invoke(app, ["a2a-card", "capture", "--card", str(card)])

    assert result.exit_code == 0, result.output
    assert "missing" in result.output


def test_an_empty_card_object_is_refused(tmp_path: Path) -> None:
    """`{}` parses, is a dict, and is still not a card — a distinct branch."""
    card = tmp_path / "card.json"
    card.write_text("{}")

    result = runner.invoke(app, ["a2a-card", "capture", "--card", str(card)])

    assert result.exit_code == 2, result.output


@pytest.mark.parametrize(
    ("content", "why"),
    [("{not json", "unparseable"), ("[1,2,3]", "not an object")],
)
def test_assure_baseline_verify_refuses_a_bad_pin_document(
    content: str, why: str, tmp_path: Path
) -> None:
    """`verify` takes two paths, so it sits outside the shared reader table."""
    pin = tmp_path / "pin.json"
    pin.write_text(content)
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: r\n")

    result = runner.invoke(app, ["assure-baseline", "verify", "--pin", str(pin),
                                 "--capsule", str(capsule), "--run", "r"])

    assert result.exit_code == 2, f"{why}: {result.output}"
