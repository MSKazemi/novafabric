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

"""ADR-0150 P3 — accountability handoff receipt (NF-189)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.hitl import (
    HandoffRecord,
    HandoffSignatureError,
    IdentityRefError,
    RecordContentError,
    SelfHandoffError,
    check_handoff_signature,
    digest_turn,
    load_handoffs,
    record_handoff,
    sign_handoff,
)
from novafabric.hitl._records import AccountabilityRecordError
from novafabric.hitl.handoff import (
    MAX_SCOPE_CODES,
    fingerprint_hex,
    handoff_signing_payload,
    same_party,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "conversation"
SCHEMA = REPO_ROOT / "schemas" / "run-capsule.schema.json"
P3 = FIXTURES / "p3-accountability-capsule.json"
INVALID = FIXTURES / "invalid-handoff-capsule.json"
THREADED = FIXTURES / "threaded-capsule.json"

KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
FP = hashlib.sha256(KEY.public_key().public_bytes_raw()).hexdigest()[:16]
HUMAN = f"human:fp:{FP}"
AGENT = "agent:spiffe://acme.example/ns/agents/sa/triager"


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _signed(**kw: Any) -> HandoffRecord:
    base: dict[str, Any] = {
        "signer": HUMAN,
        "turn_ref": "t2",
        "from_party": AGENT,
        "to_party": HUMAN,
        "at": "2026-07-15T10:00:31Z",
        "scope": ["refund_decision"],
        "reason": "above_agent_limit",
    }
    base.update(kw)
    return sign_handoff(KEY, **base)


def _ref_only(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "turn_ref": "t2",
        "from_party": HUMAN,
        "to_party": AGENT,
        "at": "2026-07-15T10:00:40Z",
        "scope": ["refund_execution"],
        "reason": "approved_within_limit",
        "sig": digest_turn("envelope"),
    }
    base.update(kw)
    return base


# ── Golden fixtures ───────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [P3, INVALID])
def test_fixtures_are_schema_valid(path: Path) -> None:
    jsonschema.validate(_load(path), json.loads(SCHEMA.read_text()))


def test_golden_handoffs_load_and_check() -> None:
    loaded = load_handoffs(_load(P3))
    assert not loaded.defects
    checks = [check_handoff_signature(r).to_dict() for _, r in loaded.records]
    assert checks == [
        {"signature": "valid", "key_binding": "fingerprint_match", "ok": True},
        {"signature": "reference_only", "key_binding": "not_applicable", "ok": True},
    ]


def test_invalid_fixture_self_handoff_and_widened_scope() -> None:
    loaded = load_handoffs(_load(INVALID))
    assert [d.index for d in loaded.defects] == [1]
    assert loaded.defects[0].error == "SelfHandoffError"
    ((_, widened),) = loaded.records
    assert check_handoff_signature(widened).signature == "invalid"


# ── Separation of duties ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "to_party",
    [
        AGENT,
        "agent:SPIFFE://ACME.EXAMPLE/ns/agents/sa/triager",
        # Full-width 's' — NFKC folds it to ASCII.
        "agent:\uff53piffe://acme.example/ns/agents/sa/triager",
    ],
)
def test_self_handoff_refused_after_normalisation(to_party: str) -> None:
    with pytest.raises(SelfHandoffError):
        HandoffRecord.model_validate(_ref_only(from_party=AGENT, to_party=to_party))


def test_system_party_refused() -> None:
    with pytest.raises(IdentityRefError):
        HandoffRecord.model_validate(_ref_only(to_party="system:scheduler"))


# ── Field validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("override", "exc"),
    [
        ({"scope": "refund"}, AccountabilityRecordError),
        ({"scope": []}, AccountabilityRecordError),
        ({"scope": ["a", "a"]}, AccountabilityRecordError),
        ({"scope": [f"s{i}" for i in range(MAX_SCOPE_CODES + 1)]}, AccountabilityRecordError),
        ({"scope": ["Refund Decision"]}, RecordContentError),
        ({"reason": "because the customer, Alice Smith, asked"}, RecordContentError),
        ({"sig": "not a signature"}, RecordContentError),
        ({"sig": "ed25519:short"}, RecordContentError),
        ({"signer_key": "ed25519:" + "0" * 64}, HandoffSignatureError),
        ({"signer_key": "ed25519:NOT-HEX"}, HandoffSignatureError),
        ({"message_body": "hi"}, RecordContentError),
        ({"from_party": "bob@example.com"}, IdentityRefError),
    ],
)
def test_invalid_fields_refused(override: dict[str, Any], exc: type[Exception]) -> None:
    with pytest.raises(exc):
        HandoffRecord.model_validate(_ref_only(**override))


def test_explicit_null_signer_fields_accepted_for_reference_sig() -> None:
    rec = HandoffRecord.model_validate(_ref_only(signer=None, signer_key=None))
    assert rec.signer is None and rec.signer_key is None


def test_ed25519_sig_requires_signer_and_key() -> None:
    body = _signed().model_dump(exclude_none=True)
    body.pop("signer_key")
    with pytest.raises(HandoffSignatureError, match="both signer"):
        HandoffRecord.model_validate(body)


def test_signer_must_be_a_party() -> None:
    body = _signed().model_dump(exclude_none=True)
    body["signer"] = "human:did:example:mallory"
    with pytest.raises(HandoffSignatureError, match="from_party or to_party"):
        HandoffRecord.model_validate(body)


# ── Signature checks ──────────────────────────────────────────────────────


def test_sign_and_verify_roundtrip_is_bound() -> None:
    rec = _signed()
    check = check_handoff_signature(rec)
    assert (check.signature, check.key_binding, check.ok) == ("valid", "fingerprint_match", True)


def test_payload_is_domain_separated_and_excludes_sig() -> None:
    rec = _signed()
    payload = handoff_signing_payload(rec.model_dump(exclude_none=True))
    assert b"novafabric.hitl.handoff.v1" in payload
    assert rec.sig.encode() not in payload


def test_extension_fields_are_signed() -> None:
    rec = _signed(ticket_ref="t-17")
    body = rec.model_dump(exclude_none=True)
    body["ticket_ref"] = "t-18"
    assert check_handoff_signature(HandoffRecord.model_validate(body)).signature == "invalid"


def test_non_fingerprint_signer_is_unbound() -> None:
    rec = _signed(signer=AGENT)
    check = check_handoff_signature(rec)
    assert (check.signature, check.key_binding, check.ok) == ("valid", "unbound", True)


def test_fingerprint_mismatch_is_not_ok() -> None:
    other = "human:fp:" + "0" * 16
    rec = _signed(signer=other, to_party=other)
    check = check_handoff_signature(rec)
    assert check.signature == "valid"
    assert check.key_binding == "fingerprint_mismatch"
    assert not check.ok


def test_garbage_signature_bytes_are_invalid() -> None:
    body = _signed().model_dump(exclude_none=True)
    body["sig"] = "ed25519:" + "A" * 86
    assert check_handoff_signature(HandoffRecord.model_validate(body)).signature == "invalid"


def test_non_curve_key_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _signed().model_dump(exclude_none=True)
    from novafabric.hitl import handoff as mod

    def boom(_: bytes) -> None:
        raise ValueError("bad key")

    monkeypatch.setattr(mod.Ed25519PublicKey, "from_public_bytes", staticmethod(boom))
    assert check_handoff_signature(HandoffRecord.model_validate(body)).signature == "invalid"


# ── Recording (fail-open) ─────────────────────────────────────────────────


def test_record_signed_and_reference_only() -> None:
    capsule = _load(THREADED)
    out = record_handoff(capsule, _signed())
    assert out.recorded
    out2 = record_handoff(out.capsule, _ref_only())
    assert out2.recorded
    assert len(out2.capsule["facets"]["conversation"]["handoff"]) == 2


def test_record_refuses_bad_signature() -> None:
    capsule = _load(P3)
    body = _signed().model_dump(exclude_none=True)
    body["scope"] = ["account_closure"]
    out = record_handoff(capsule, body)
    assert not out.recorded and out.capsule is capsule
    assert out.reason == "handoff signature does not verify"


def test_record_self_handoff_never_raises() -> None:
    capsule = _load(P3)
    out = record_handoff(capsule, _ref_only(to_party=HUMAN))
    assert not out.recorded and out.reason == "SelfHandoffError"


def test_record_dangling_turn_not_recorded() -> None:
    capsule = _load(P3)
    out = record_handoff(capsule, _ref_only(turn_ref="t404"))
    assert not out.recorded and out.reason == "turn_ref does not resolve"


# ── SoD across fingerprint spellings (reviewer defect 3) ──────────────────

FULL_FP = hashlib.sha256(KEY.public_key().public_bytes_raw()).hexdigest()


def test_poc_fingerprint_prefix_self_handoff_refused() -> None:
    """Reviewer PoC: A (16 hex) -> A' (32 hex) is one key under two names."""
    long_ref = f"human:fp:{FULL_FP[:32]}"
    with pytest.raises(SelfHandoffError):
        _signed(from_party=HUMAN, to_party=long_ref)
    body = {**_ref_only(from_party=HUMAN, to_party=long_ref)}
    out = record_handoff(_load(P3), body)
    assert not out.recorded and out.reason == "SelfHandoffError"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (f"human:fp:{FULL_FP[:16]}", f"human:fp:{FULL_FP}"),
        (f"human:fp:{FULL_FP[:40]}", f"human:fp:{FULL_FP[:20].upper()}"),
        # One key cannot be two parties just by switching the kind label.
        (f"human:fp:{FULL_FP[:16]}", f"agent:fp:{FULL_FP[:24]}"),
    ],
)
def test_fingerprint_prefix_is_same_party(a: str, b: str) -> None:
    assert same_party(a, b) and same_party(b, a)
    with pytest.raises(SelfHandoffError):
        HandoffRecord.model_validate(_ref_only(from_party=a, to_party=b))


def test_distinct_fingerprints_still_distinct() -> None:
    other = "human:fp:" + ("0" if FULL_FP[16] != "0" else "1") + FULL_FP[1:24]
    assert not same_party(HUMAN, other)
    assert not same_party(HUMAN, AGENT)
    assert HandoffRecord.model_validate(_ref_only(from_party=HUMAN, to_party=other))


def test_both_parties_matching_signer_key_refused() -> None:
    """Both refs prefix sha256(signer_key) -> prefixes of each other -> refused."""
    with pytest.raises(SelfHandoffError):
        _signed(
            signer=f"human:fp:{FULL_FP[:20]}",
            from_party=f"human:fp:{FULL_FP[:20]}",
            to_party=f"agent:fp:{FULL_FP[:48]}",
        )


def test_signer_may_use_longer_spelling_of_its_party() -> None:
    rec = _signed(signer=f"human:fp:{FULL_FP}")
    assert check_handoff_signature(rec).key_binding == "fingerprint_match"


@pytest.mark.parametrize(
    "ref", ["human:fp:abcd1234", "agent:fp:" + "a" * 65, "human:FP:xyz0123456789abcd"]
)
def test_malformed_fingerprint_ref_refused(ref: str) -> None:
    with pytest.raises(IdentityRefError):
        HandoffRecord.model_validate(_ref_only(to_party=ref))
    assert fingerprint_hex(ref) is None
