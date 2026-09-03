"""nova a2a-card — CLI surface for ADR-0149 D1 / NF-171."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

CARD = {
    "name": "planner",
    "provider": {"organization": "acme"},
    "url": "https://acme.example/a2a",
    "skills": [{"id": "plan", "name": "Plan tasks"}],
    "capabilities": {"streaming": True},
    "securitySchemes": {"oauth2": {"type": "oauth2"}},
    "signature": {"alg": "EdDSA", "sig": "deadbeef"},
}


@pytest.fixture()
def card_file(tmp_path: Path) -> Path:
    p = tmp_path / "card.json"
    p.write_text(json.dumps(CARD))
    return p


def test_capture_writes_a_facet_with_the_full_card(
    card_file: Path, tmp_path: Path
) -> None:
    out = tmp_path / "facet.json"
    result = runner.invoke(app, [
        "a2a-card", "capture", "--card", str(card_file), "--out", str(out),
        "--well-known-url", "https://acme.example/.well-known/agent.json",
    ])

    assert result.exit_code == 0, result.output
    facet = json.loads(out.read_text())
    assert facet["card"] == CARD
    assert facet["card_fingerprint"].startswith("sha256:")
    assert facet["signed"] is True


def test_capture_does_not_claim_the_signature_is_valid(
    card_file: Path, tmp_path: Path
) -> None:
    """The card has a signature block; nothing verified it."""
    out = tmp_path / "facet.json"
    runner.invoke(app, ["a2a-card", "capture", "--card", str(card_file),
                        "--out", str(out)])

    facet = json.loads(out.read_text())
    assert "signature_ok" not in facet, "an unchecked signature must not be reported"
    assert "unverified" in facet["signature_status"]


def test_capture_writes_the_portable_export(card_file: Path, tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    result = runner.invoke(app, [
        "a2a-card", "capture", "--card", str(card_file),
        "--outputs", str(outputs), "--out", str(tmp_path / "facet.json"),
    ])

    assert result.exit_code == 0, result.output
    exported = list(outputs.glob("a2a-card-*.json"))
    assert len(exported) == 1
    assert json.loads(exported[0].read_text()) == CARD


def test_verify_passes_on_an_untouched_facet(card_file: Path, tmp_path: Path) -> None:
    facet = tmp_path / "facet.json"
    runner.invoke(app, ["a2a-card", "capture", "--card", str(card_file),
                        "--out", str(facet)])

    result = runner.invoke(app, ["a2a-card", "verify", "--facet", str(facet)])
    assert result.exit_code == 0, result.output


def test_verify_exits_one_when_the_card_was_altered(
    card_file: Path, tmp_path: Path
) -> None:
    facet_path = tmp_path / "facet.json"
    runner.invoke(app, ["a2a-card", "capture", "--card", str(card_file),
                        "--out", str(facet_path)])

    doc = json.loads(facet_path.read_text())
    doc["card"]["name"] = "impostor"
    facet_path.write_text(json.dumps(doc))

    result = runner.invoke(app, ["a2a-card", "verify", "--facet", str(facet_path)])
    assert result.exit_code == 1, result.output


def test_a_missing_card_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["a2a-card", "capture", "--card",
                                 str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_a_non_object_card_exits_two(tmp_path: Path) -> None:
    p = tmp_path / "card.json"
    p.write_text("[1,2,3]")
    result = runner.invoke(app, ["a2a-card", "capture", "--card", str(p)])
    assert result.exit_code == 2
