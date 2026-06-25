"""Tests for the auto-generated SEALED system/audit card (ADR-0085, gap-014).

The card is generated from capsule + eval + lineage facts and sealed via the
existing DSSE/Ed25519 path (no new crypto). It must verify with the existing
verifier.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from novafabric.compliance.system_card import (
    SYSTEM_CARD_PREDICATE_TYPE,
    SystemCardGenerator,
)
from novafabric.evidence.intoto import dsse_verify
from novafabric.evidence.signing import (
    LocalSigner,
    generate_keypair,
    verify_with_pem,
)


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    cap = tmp_path / "01HCAPSULE000000000000000"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(
        yaml.safe_dump(
            {
                "run_id": "run-abc-123",
                "model": "claude-sonnet-4-6",
                "provider": "anthropic",
                "model_version": "2026-05-01",
            }
        )
    )
    (cap / "eval_result.json").write_text(
        json.dumps(
            {
                "suite_id": "gaia-level-1",
                "passed": True,
                "metrics": [{"name": "accuracy", "value": 0.72}],
            }
        )
    )
    (cap / "lineage.jsonl").write_text(
        '{"from":"a","to":"b"}\n{"from":"b","to":"c"}\n'
    )
    return cap


@pytest.fixture
def signer(tmp_path: Path) -> LocalSigner:
    priv_path, _pub = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv_path)


def test_card_contains_capsule_facts(capsule_dir: Path) -> None:
    card = SystemCardGenerator().build_card(capsule_dir)
    assert card["run_id"] == "run-abc-123"
    assert card["model"]["name"] == "claude-sonnet-4-6"
    assert card["model"]["provider"] == "anthropic"
    assert card["model"]["version"] == "2026-05-01"


def test_card_contains_eval_facts(capsule_dir: Path) -> None:
    card = SystemCardGenerator().build_card(capsule_dir)
    assert card["evaluation"]["passed"] is True
    assert card["evaluation"]["suite_id"] == "gaia-level-1"


def test_card_contains_lineage_facts(capsule_dir: Path) -> None:
    card = SystemCardGenerator().build_card(capsule_dir)
    assert card["lineage"]["edge_count"] == 2


def test_card_is_generated_not_hand_written(capsule_dir: Path) -> None:
    # Every card records that it was machine-generated from facts.
    card = SystemCardGenerator().build_card(capsule_dir)
    assert card["generated_by"]["tool"] == "novafabric"
    assert "generated_at" in card


def test_seal_produces_dsse_envelope(capsule_dir: Path, signer: LocalSigner) -> None:
    gen = SystemCardGenerator()
    envelope = gen.seal_card(gen.build_card(capsule_dir), signer)
    assert envelope["payloadType"] == "application/vnd.in-toto+json"
    assert envelope["signatures"][0]["keyid"] == signer.keyid


def test_sealed_card_verifies_with_existing_verifier(
    capsule_dir: Path, signer: LocalSigner
) -> None:
    gen = SystemCardGenerator()
    envelope = gen.seal_card(gen.build_card(capsule_dir), signer)

    statement = dsse_verify(
        envelope,
        lambda pae, sig: verify_with_pem(signer.public_pem, pae, sig),
    )
    assert statement["predicateType"] == SYSTEM_CARD_PREDICATE_TYPE
    assert statement["predicate"]["run_id"] == "run-abc-123"
    assert statement["predicate"]["evaluation"]["passed"] is True


def test_tampered_card_fails_verification(
    capsule_dir: Path, signer: LocalSigner
) -> None:
    import base64

    gen = SystemCardGenerator()
    envelope = gen.seal_card(gen.build_card(capsule_dir), signer)

    # Tamper with the payload after signing.
    tampered = json.loads(base64.b64decode(envelope["payload"]))
    tampered["predicate"]["run_id"] = "EVIL"
    envelope["payload"] = base64.b64encode(
        json.dumps(tampered).encode()
    ).decode()

    with pytest.raises(ValueError):
        dsse_verify(
            envelope,
            lambda pae, sig: verify_with_pem(signer.public_pem, pae, sig),
        )


def test_card_handles_missing_optional_files(
    tmp_path: Path, signer: LocalSigner
) -> None:
    cap = tmp_path / "minimal"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump({"run_id": "r1", "model": "m"}))

    gen = SystemCardGenerator()
    card = gen.build_card(cap)
    assert card["run_id"] == "r1"
    assert card["evaluation"] is None
    assert card["lineage"]["edge_count"] == 0
    # Still seals + verifies even with no eval/lineage.
    envelope = gen.seal_card(card, signer)
    statement = dsse_verify(
        envelope,
        lambda pae, sig: verify_with_pem(signer.public_pem, pae, sig),
    )
    assert statement["predicate"]["run_id"] == "r1"
