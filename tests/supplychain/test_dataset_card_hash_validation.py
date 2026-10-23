"""L32: a dataset card's content hash must be a full SHA-256 before it is signed.

``build_card`` stripped an optional ``sha256:`` prefix and stored whatever was left, so
``--hash b17a`` (the truncated form the help example shows as ``b17a...``) produced a
card whose signature vouched for a digest that identifies nothing. The card schema
requires 64 lowercase hex digits. The same applies to ``--registry-digest``.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.supplychain.dataset_card import (
    DatasetCardError,
    DatasetProvenanceCard,
    build_card,
)

runner = CliRunner()

FULL = "b17a" + "0" * 60
SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "features"
        / "dataset-provenance-card-v0.schema.json"
    ).read_text()
)


def _build(dataset_hash: str, registry_digest: str | None = None) -> DatasetProvenanceCard:
    return build_card(
        asset="dataset:x@1",
        source_uri="oci://x",
        version="1",
        dataset_hash=dataset_hash,
        registry_digest=registry_digest,
    )


@pytest.mark.parametrize("value", [FULL, f"sha256:{FULL}", FULL.upper(), f"sha256:{FULL.upper()}"])
def test_full_digest_accepted_in_every_spelling(value: str) -> None:
    card = _build(value)
    assert card.hash.sha256 == FULL
    jsonschema.Draft202012Validator(SCHEMA).validate(
        {**card.to_json_dict(), "signature": {"keyid": "k", "alg": "ed25519", "sig": "AA=="}}
    )


@pytest.mark.parametrize(
    "value",
    ["b17a", "b17a...", f"sha256:{FULL[:63]}", FULL + "0", "z" * 64, "", "sha256:", f"md5:{FULL}"],
)
def test_truncated_or_malformed_digest_refused(value: str) -> None:
    with pytest.raises(DatasetCardError, match="64 hex"):
        _build(value)


def test_registry_digest_is_validated_too() -> None:
    with pytest.raises(DatasetCardError, match="registry"):
        _build(FULL, registry_digest="abc")
    assert _build(FULL, registry_digest=f"sha256:{FULL}").bound_to is not None


def test_model_refuses_a_short_digest() -> None:
    """A card parsed back from JSON is held to the same rule as the schema."""
    with pytest.raises(ValueError):
        DatasetProvenanceCard.model_validate(
            {"asset": "a", "source": {"uri": "u"}, "version": "1", "hash": {"sha256": "b17a"}}
        )


def test_cli_refuses_a_truncated_hash_and_signs_nothing(tmp_path: Path) -> None:
    out = tmp_path / "card.json"
    result = runner.invoke(
        app,
        ["dataset", "provenance-card", "dataset:x@1", "--source", "oci://x",
         "--version", "1", "--hash", "b17a", "--sign", "--identity", "nf-test-ds",
         "--out", str(out)],
    )
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert "--hash" in result.output and "64 hex" in result.output
    assert not out.exists()
