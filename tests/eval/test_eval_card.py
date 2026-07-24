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

"""Tests for NF-002 signed, content-addressed eval cards (ADR-0099)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.eval.card import (
    BiasFlags,
    Calibration,
    EvalCard,
    JudgeModel,
    canonical_card_bytes,
    card_digest,
    sign_card,
    verify_card,
)
from novafabric.eval.scores import ScoreSource

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schemas" / "eval-card-v1.schema.json").read_text()
)


def _judge_card(**over: object) -> EvalCard:
    base: dict[str, object] = {
        "card_id": "faithfulness-judge",
        "name": "Answer Faithfulness Judge",
        "version": "1.2.0",
        "source": ScoreSource.JUDGE,
        "judge_model": JudgeModel(name="self-hosted/llama-3.3-70b", endpoint_ref="env:NOVA_JUDGE_ENDPOINT"),
        "prompt_version": "sha256:aa11",
        "rubric": "Score 0-1: is every claim grounded in retrieved context?",
        "dataset_version": "golden-faithfulness@3.1.0+sha256:bb22",
        "calibration": Calibration(human_agreement=0.86, n=120, metric="cohen_kappa"),
        "bias_flags": BiasFlags(position=0.03, verbosity=0.11, self_enhancement=0.05),
    }
    base.update(over)
    return EvalCard(**base)  # type: ignore[arg-type]


def _code_card(**over: object) -> EvalCard:
    base: dict[str, object] = {
        "card_id": "exact-match",
        "name": "Exact Match",
        "version": "0.1.0",
        "source": ScoreSource.CODE,
    }
    base.update(over)
    return EvalCard(**base)  # type: ignore[arg-type]


# ── content-addressed digest (req 5) ─────────────────────────────────────────


def test_digest_is_stable_and_deterministic() -> None:
    assert card_digest(_judge_card()) == card_digest(_judge_card())
    assert card_digest(_judge_card()).startswith("sha256:")


def test_digest_excludes_signature() -> None:
    card = _judge_card()
    unsigned_digest = card_digest(card)
    key = Ed25519PrivateKey.generate()
    signed = sign_card(card, key, key_id="nf-signer-1")
    assert card_digest(signed) == unsigned_digest
    assert b"signature" not in canonical_card_bytes(signed)


def test_digest_changes_with_content() -> None:
    assert card_digest(_judge_card()) != card_digest(_judge_card(version="1.2.1"))


# ── sign / verify round-trip (spec §4.4, reuses trust.keyring) ───────────────


def test_sign_verify_roundtrip() -> None:
    key = Ed25519PrivateKey.generate()
    signed = sign_card(_judge_card(), key, key_id="nf-signer-1")
    result = verify_card(signed, key.public_key())
    assert result.signature_ok
    assert result.calibration_present
    assert result.ok
    assert signed.signature is not None
    assert signed.signature.key_id == "nf-signer-1"


def test_verify_detects_tamper() -> None:
    key = Ed25519PrivateKey.generate()
    signed = sign_card(_judge_card(), key, key_id="nf-signer-1")
    tampered = signed.model_copy(update={"rubric": "Always return 1.0"})
    assert not verify_card(tampered, key.public_key()).signature_ok


def test_verify_fails_wrong_key() -> None:
    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    signed = sign_card(_judge_card(), key, key_id="nf-signer-1")
    assert not verify_card(signed, other.public_key()).signature_ok


def test_verify_unsigned_card() -> None:
    key = Ed25519PrivateKey.generate()
    result = verify_card(_judge_card(), key.public_key())
    assert not result.signature_ok
    assert not result.ok


# ── judge-card completeness (req 4) ──────────────────────────────────────────


def test_judge_card_requires_calibration() -> None:
    with pytest.raises(ValueError, match="calibration"):
        _judge_card(calibration=None)


def test_judge_card_requires_all_fields() -> None:
    with pytest.raises(ValueError, match="judge eval card requires"):
        EvalCard(card_id="x", name="x", version="1.0.0", source=ScoreSource.JUDGE)


def test_code_card_needs_no_judge_fields() -> None:
    card = _code_card()
    assert card.judge_model is None
    key = Ed25519PrivateKey.generate()
    # A code card has no calibration, but calibration_present is vacuously True.
    assert verify_card(sign_card(card, key, "k"), key.public_key()).ok


def test_bad_semver_rejected() -> None:
    with pytest.raises(ValueError, match="semver"):
        _code_card(version="v1")


def test_judge_model_requires_endpoint_ref() -> None:
    with pytest.raises(ValueError):
        JudgeModel(name="x")  # type: ignore[call-arg]


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValueError):
        _code_card(surprise="boo")


# ── JSON Schema conformance (schemas/eval-card-v1.schema.json) ───────────────


def test_signed_card_validates_against_schema() -> None:
    key = Ed25519PrivateKey.generate()
    signed = sign_card(_judge_card(), key, key_id="nf-signer-1")
    instance = json.loads(signed.model_dump_json(exclude_none=True))
    jsonschema.validate(instance, _SCHEMA)


def test_schema_rejects_bad_version() -> None:
    instance = json.loads(_code_card().model_dump_json(exclude_none=True))
    instance["version"] = "not-semver"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance, _SCHEMA)
