# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for NF-058 signed dataset provenance cards (ADR-0105)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.supplychain.dataset_card import (
    CARD_SCHEMA_VERSION,
    build_card,
    canonical_card_bytes,
    sign_card,
    transforms_from_capsule,
    verify_card,
)
from novafabric.trust.keyring import ensure_keypair

runner = CliRunner()

_SHA = "b17a" + "0" * 60
SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "features"
        / "dataset-provenance-card-v0.schema.json"
    ).read_text()
)


def _validate(card_dict: dict) -> None:
    jsonschema.Draft202012Validator(SCHEMA).validate(card_dict)


def _card():
    return build_card(
        asset="dataset:gaia@2026-05",
        source_uri="oci://reg/gaia:2026-05",
        version="2026-05",
        dataset_hash=_SHA,
        retrieved_at="2026-05-07T00:00:00Z",
        license="CC-BY-4.0",
        tlp="TLP:CLEAR",
        registry_digest=_SHA,
    )


# ── schema (req 8/9) ─────────────────────────────────────────────────────────


def test_schema_is_meta_valid() -> None:
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_unsigned_card_missing_signature_is_schema_invalid() -> None:
    # signature is required by the schema — an unsigned card is not evidence.
    assert not jsonschema.Draft202012Validator(SCHEMA).is_valid(_card().to_json_dict())


def test_signed_card_validates_against_schema() -> None:
    priv, fp = ensure_keypair("nf-test-ds")
    signed = sign_card(_card(), priv, keyid=fp)
    _validate(signed.to_json_dict())
    assert signed.to_json_dict()["schemaVersion"] == CARD_SCHEMA_VERSION


# ── signing round-trip (req 9) ───────────────────────────────────────────────


def test_sign_and_verify_roundtrip() -> None:
    priv, fp = ensure_keypair("nf-test-ds")
    signed = sign_card(_card(), priv, keyid=fp)
    assert verify_card(signed, priv.public_key())


def test_unsigned_card_does_not_verify() -> None:
    priv, _ = ensure_keypair("nf-test-ds")
    assert not verify_card(_card(), priv.public_key())


def test_tamper_breaks_signature() -> None:
    priv, fp = ensure_keypair("nf-test-ds")
    signed = sign_card(_card(), priv, keyid=fp)
    signed.version = "tampered"
    assert not verify_card(signed, priv.public_key())


def test_signature_excluded_from_canonical_bytes() -> None:
    priv, fp = ensure_keypair("nf-test-ds")
    card = _card()
    before = canonical_card_bytes(card)
    signed = sign_card(card, priv, keyid=fp)
    assert canonical_card_bytes(signed) == before  # signing doesn't change the signed body


# ── transform history from lineage (req 9 — digests, never values) ───────────


def test_transforms_from_capsule() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cap = Path(td)
        (cap / "lineage.jsonl").write_text(
            json.dumps({"edge_type": "produced_by", "edge_id": "e1", "op_type": "tool_call/filter"})
            + "\n"
            + json.dumps({"edge_type": "nominal", "edge_id": "e2"})  # ignored
            + "\n"
        )
        transforms = transforms_from_capsule(cap)
    assert len(transforms) == 1
    t = transforms[0].model_dump(by_alias=True, exclude_none=True)
    assert t["op"] == "produced_by"
    assert t["signedOpDigest"].startswith("sha256:")
    assert t["opType"] == "tool_call/filter"
    assert t["producedEdge"] == "e1"


def test_transforms_from_capsule_no_lineage(tmp_path: Path) -> None:
    assert transforms_from_capsule(tmp_path) == []


def test_transforms_skips_malformed_and_blank_lines(tmp_path: Path) -> None:
    (tmp_path / "lineage.jsonl").write_text(
        "\n"  # blank
        "not json\n"  # malformed
        + json.dumps({"edge_type": "derived", "edge_id": "e9"})
        + "\n"
    )
    transforms = transforms_from_capsule(tmp_path)
    assert len(transforms) == 1
    t = transforms[0].model_dump(by_alias=True, exclude_none=True)
    assert t["op"] == "derived"
    assert "opType" not in t and "producedEdge" in t


def test_card_with_transforms_validates() -> None:
    import tempfile

    priv, fp = ensure_keypair("nf-test-ds")
    with tempfile.TemporaryDirectory() as td:
        cap = Path(td)
        (cap / "lineage.jsonl").write_text(
            json.dumps({"edge_type": "transformed", "edge_id": "e1"}) + "\n"
        )
        transforms = transforms_from_capsule(cap)
    card = build_card(
        asset="dataset:x@1", source_uri="oci://x", version="1",
        dataset_hash=_SHA, transforms=transforms, registry_digest=_SHA,
    )
    signed = sign_card(card, priv, keyid=fp)
    _validate(signed.to_json_dict())


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_emits_signed_card(tmp_path: Path) -> None:
    out = tmp_path / "card.json"
    result = runner.invoke(
        app,
        ["dataset", "provenance-card", "dataset:gaia@2026-05",
         "--source", "oci://reg/gaia:2026-05", "--version", "2026-05",
         "--hash", _SHA, "--license", "CC-BY-4.0", "--tlp", "TLP:CLEAR",
         "--registry-digest", _SHA, "--sign", "--identity", "nf-test-ds",
         "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    card = json.loads(out.read_text())
    _validate(card)
    assert card["signature"]["alg"] == "ed25519"


def test_cli_unsigned_to_stdout(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["dataset", "provenance-card", "dataset:x@1", "--source", "oci://x",
         "--version", "1", "--hash", _SHA],
    )
    assert result.exit_code == 0, result.output
    assert "dataset:x@1" in result.output


def test_cli_from_capsule_transforms(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "lineage.jsonl").write_text(
        json.dumps({"edge_type": "produced_by", "edge_id": "e1"}) + "\n"
    )
    out = tmp_path / "card.json"
    result = runner.invoke(
        app,
        ["dataset", "provenance-card", "dataset:x@1", "--source", "oci://x",
         "--version", "1", "--hash", _SHA, "--from-capsule", str(cap),
         "--registry-digest", _SHA, "--sign", "--identity", "nf-test-ds",
         "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    card = json.loads(out.read_text())
    assert card["transformHistory"][0]["op"] == "produced_by"


def test_cli_help_smoke() -> None:
    result = runner.invoke(app, ["dataset", "provenance-card", "--help"])
    assert result.exit_code == 0
    assert "provenance" in result.output.lower()
