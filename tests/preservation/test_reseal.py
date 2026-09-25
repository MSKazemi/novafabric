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

"""NF-333 crypto re-seal record + NF-334 LTV renewal chain (ADR-0165 P3)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from novafabric.preservation import (
    CRYPTO_MIGRATION_FIELD,
    HASH_STRENGTH,
    LTV_CHAIN_FIELD,
    SIGNATURE_STRENGTH,
    BrokenLtvChainError,
    BrokenResealRecordError,
    CryptoMigrationEvent,
    LtvRenewal,
    OriginalSignatureDroppedError,
    P3RewriteError,
    PreservationError,
    PreservationFacet,
    append_crypto_migration,
    append_ltv_renewal,
    crypto_migrations_from_facet,
    ltv_chain_from_facet,
    plan_ltv_renewal,
    plan_reseal,
    verify_crypto_migrations,
    verify_ltv_chain,
    verify_p3_append_only,
)
from novafabric.preservation import reseal as reseal_mod

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "preservation"


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def _facet(name: str = "valid-anchor.json") -> PreservationFacet:
    return PreservationFacet.model_validate(_load(name))


def d256(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


def d3(s: str) -> str:
    return "sha3-256:" + hashlib.sha3_256(s.encode()).hexdigest()


def d512(s: str) -> str:
    return "sha512:" + hashlib.sha512(s.encode()).hexdigest()


def _event(**over: Any) -> CryptoMigrationEvent:
    base: dict[str, Any] = {
        "from_alg": "ed25519",
        "to_alg": "ml-dsa-65",
        "resealed_at": "2029-06-01T00:00:00Z",
        "upgrade_ref": "NF-192:upgrade-signature#op-1",
        "original_sig_preserved": True,
        "renewal_timestamp_ref": d256("tst-1"),
    }
    base.update(over)
    return CryptoMigrationEvent.model_validate(base)


def _renewal(**over: Any) -> LtvRenewal:
    base: dict[str, Any] = {
        "renewal_type": "timestamp_renewal",
        "covered_digest": d256("evidence"),
        "new_timestamp_ref": d256("tst-a"),
        "new_hash_alg": "sha256",
        "renewed_before": "2030-01-01",
        "parent": None,
    }
    base.update(over)
    return LtvRenewal.model_validate(base)


def _codes(v: Any) -> set[str]:
    return {f.code for f in v.findings}


# ── Golden fixtures ───────────────────────────────────────────────────────


def test_valid_fixture_verifies() -> None:
    facet = _facet("valid-reseal-ltv-chain.json")
    reseal = verify_crypto_migrations(crypto_migrations_from_facet(facet))
    assert reseal.ok and reseal.scheme_migrations == ["ed25519→ml-dsa-65"]
    ltv = verify_ltv_chain(ltv_chain_from_facet(facet))
    assert ltv.ok and ltv.renewal_count == 2


def test_invalid_fixture_original_sig_dropped_is_rejected() -> None:
    facet = _facet("invalid-reseal-original-sig-dropped.json")
    with pytest.raises(OriginalSignatureDroppedError):
        crypto_migrations_from_facet(facet)


def test_invalid_fixture_ltv_downgrade_reports_every_fault() -> None:
    v = verify_ltv_chain(ltv_chain_from_facet(_facet("invalid-ltv-chain-downgrade.json")))
    assert not v.ok
    assert {"missing_parent", "hash_alg_downgrade", "renewed_before_not_monotonic"} <= _codes(v)
    assert not v.covers_previous and not v.hash_algs_ok and not v.renewed_in_time


def test_pre_p3_facet_has_empty_records() -> None:
    facet = _facet()
    assert crypto_migrations_from_facet(facet) == []
    assert ltv_chain_from_facet(facet) == []
    assert verify_ltv_chain([]).ok and verify_crypto_migrations([]).ok


# ── NF-333 model ──────────────────────────────────────────────────────────


def test_original_sig_preserved_is_a_required_literal() -> None:
    with pytest.raises(ValidationError):
        _event(original_sig_preserved=False)
    data = _event().model_dump()
    del data["original_sig_preserved"]
    with pytest.raises(ValidationError):
        CryptoMigrationEvent.model_validate(data)


@pytest.mark.parametrize(
    "ref",
    [
        "https://user:pw@tsa.example.org/tok",
        "-----BEGIN " + "PRIVATE KEY-----",  # split: keep secret scanners quiet
        "alice:hunter2@host",
        "x" * 3000,
        b"raw-bytes",
    ],
)
def test_refs_refuse_secrets_and_payloads(ref: object) -> None:
    with pytest.raises(ValidationError):
        _event(upgrade_ref=ref)


def test_refs_accept_opaque_uri_and_digest() -> None:
    assert _event(upgrade_ref="https://ops.example.org/op/1").upgrade_ref.startswith("https")
    assert _event(renewal_timestamp_ref=d256("x")).renewal_timestamp_ref.startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "value"),
    [("from_alg", "Ed25519"), ("resealed_at", "2029-06-01T00:00:00"), ("resealed_at", "nope")],
)
def test_event_shape_errors(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _event(**{field: value})


def test_malformed_event_in_facet_raises() -> None:
    data = _load("valid-anchor.json")
    data[CRYPTO_MIGRATION_FIELD] = [{"original_sig_preserved": True, "from_alg": "ed25519"}]
    with pytest.raises(PreservationError, match="malformed"):
        crypto_migrations_from_facet(PreservationFacet.model_validate(data))
    data[CRYPTO_MIGRATION_FIELD] = {"not": "a list"}
    with pytest.raises(PreservationError, match="must be a list"):
        crypto_migrations_from_facet(PreservationFacet.model_validate(data))
    data[CRYPTO_MIGRATION_FIELD] = ["not-a-dict"]
    with pytest.raises(OriginalSignatureDroppedError):
        crypto_migrations_from_facet(PreservationFacet.model_validate(data))


def test_oversized_list_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reseal_mod, "MAX_RECORDS", 1)
    data = _load("valid-reseal-ltv-chain.json")
    with pytest.raises(PreservationError, match="limit"):
        ltv_chain_from_facet(PreservationFacet.model_validate(data))


# ── NF-333 verification ───────────────────────────────────────────────────


def test_reseal_findings() -> None:
    events = [
        _event(),
        _event(from_alg="ed448", to_alg="ed448", resealed_at="2028-01-01T00:00:00Z"),
    ]
    assert {
        "alg_discontinuity",
        "no_alg_change",
        "time_not_monotonic",
        "renewal_timestamp_reused",
    } <= (_codes(verify_crypto_migrations(events)))


def test_reseal_pqc_to_classic_and_unknown_alg() -> None:
    v = verify_crypto_migrations([_event(from_alg="ml-dsa-65", to_alg="ed25519")])
    assert _codes(v) == {"pqc_to_classic_downgrade"}
    v = verify_crypto_migrations([_event(to_alg="future-sig-9")])
    assert _codes(v) == {"unknown_signature_alg"}
    assert not v.ok


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("renewal_timestamp_ref", "ts-1\n"),
        ("upgrade_ref", "op-1\n"),
        ("renewal_timestamp_ref", d256("x") + "\n"),
        ("from_alg", "ed25519\n"),
        ("to_alg", "ml-dsa-65\n"),
    ],
)
def test_trailing_newline_is_refused(field: str, value: str) -> None:
    """Regression: ``^…$`` accepted ``"ts-1\\n"`` and let a reused timestamp slip by."""
    with pytest.raises(ValidationError):
        _event(**{field: value})


def test_trailing_newline_cannot_bypass_renewal_timestamp_reuse() -> None:
    first = _event(renewal_timestamp_ref="ts-1")
    with pytest.raises(ValidationError):
        _event(
            from_alg="ml-dsa-65",
            to_alg="ml-dsa-87",
            resealed_at="2031-01-01T00:00:00Z",
            renewal_timestamp_ref="ts-1\n",
        )
    second = _event(
        from_alg="ml-dsa-65",
        to_alg="ml-dsa-87",
        resealed_at="2031-01-01T00:00:00Z",
        renewal_timestamp_ref="ts-1",
    )
    v = verify_crypto_migrations([first, second])
    assert _codes(v) == {"renewal_timestamp_reused"} and not v.ok


def test_ltv_digest_refuses_trailing_newline() -> None:
    with pytest.raises(ValidationError):
        _renewal(new_timestamp_ref=d256("tst-a") + "\n")


@pytest.mark.parametrize(
    ("from_alg", "to_alg"),
    [
        ("ml-dsa-87", "ml-dsa-44"),
        ("ml-dsa-65", "slh-dsa-sha2-128s"),
        ("slh-dsa-shake-256f", "ml-dsa-65"),
        ("ed25519", "rsa-pkcs1-2048"),
        ("ecdsa-p384", "ecdsa-p256"),
        ("ed448", "rsa-pss-4096"),
    ],
)
def test_reseal_within_tier_downgrade_is_a_finding(from_alg: str, to_alg: str) -> None:
    v = verify_crypto_migrations([_event(from_alg=from_alg, to_alg=to_alg)])
    assert _codes(v) == {"signature_alg_downgrade"} and not v.ok


@pytest.mark.parametrize(
    ("from_alg", "to_alg"),
    [
        ("ed25519", "ml-dsa-44"),  # any PQC outranks any classic
        ("ml-dsa-44", "ml-dsa-87"),
        ("ml-dsa-65", "slh-dsa-sha2-192s"),  # equal NIST category: lateral
        ("rsa-pkcs1-3072", "ed25519"),  # equal classical bits: lateral
        ("rsa-pkcs1-2048", "rsa-pss-2048"),
    ],
)
def test_reseal_upgrade_or_lateral_is_clean(from_alg: str, to_alg: str) -> None:
    assert verify_crypto_migrations([_event(from_alg=from_alg, to_alg=to_alg)]).ok


def test_pqc_to_classic_reports_one_downgrade_code() -> None:
    v = verify_crypto_migrations([_event(from_alg="ml-dsa-44", to_alg="ecdsa-p521")])
    assert _codes(v) == {"pqc_to_classic_downgrade"}


def test_signature_strength_order_is_explicit() -> None:
    assert all(tier in (0, 1) for tier, _ in SIGNATURE_STRENGTH.values())
    assert max(r for r in SIGNATURE_STRENGTH.values() if r[0] == 0) < min(
        r for r in SIGNATURE_STRENGTH.values() if r[0] == 1
    )
    assert SIGNATURE_STRENGTH["ml-dsa-44"] < SIGNATURE_STRENGTH["ml-dsa-65"]
    assert SIGNATURE_STRENGTH["ml-dsa-65"] < SIGNATURE_STRENGTH["ml-dsa-87"]
    assert SIGNATURE_STRENGTH["rsa-pkcs1-2048"] < SIGNATURE_STRENGTH["ed25519"]
    assert "rsa-pkcs1-1024" not in SIGNATURE_STRENGTH and "dsa" not in SIGNATURE_STRENGTH


def test_reseal_model_construct_cannot_bypass_literal() -> None:
    forged = CryptoMigrationEvent.model_construct(
        **{**_event().model_dump(), "original_sig_preserved": False}
    )
    v = verify_crypto_migrations([forged])
    assert not v.original_sig_preserved and not v.ok


def test_reseal_too_many(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reseal_mod, "MAX_RECORDS", 1)
    v = verify_crypto_migrations([_event(), _event()])
    assert _codes(v) == {"too_many_records"} and not v.ok


def test_plan_and_append_reseal_chain() -> None:
    facet = _facet()
    with pytest.raises(PreservationError, match="from_alg"):
        plan_reseal(
            facet,
            to_alg="ml-dsa-65",
            upgrade_ref="op",
            renewal_timestamp_ref=d256("t"),
            resealed_at="2029-01-01T00:00:00Z",
        )
    first = plan_reseal(
        facet,
        from_alg="ed25519",
        to_alg="ml-dsa-65",
        upgrade_ref="op-1",
        renewal_timestamp_ref=d256("t1"),
        resealed_at="2029-01-01T00:00:00Z",
    )
    f1 = append_crypto_migration(facet, first, agent_ref="https://agents.example.org/a")
    assert f1.provenance_events[-1].event == "digital signature generation"
    assert facet.model_extra == {}  # input untouched
    second = plan_reseal(
        f1,
        to_alg="ml-dsa-87",
        upgrade_ref="op-2",
        renewal_timestamp_ref=d256("t2"),
        resealed_at="2031-01-01T00:00:00Z",
    )
    assert second.from_alg == "ml-dsa-65"
    f2 = append_crypto_migration(f1, second, record_event=False)
    assert len(f2.provenance_events) == len(f1.provenance_events)
    verify_p3_append_only(f1, f2)
    with pytest.raises(PreservationError, match="contradicts"):
        plan_reseal(
            f2,
            from_alg="ed25519",
            to_alg="ml-dsa-87",
            upgrade_ref="op",
            renewal_timestamp_ref=d256("t3"),
            resealed_at="2032-01-01T00:00:00Z",
        )
    # would break: reuse a timestamp
    bad = _event(
        from_alg="ml-dsa-87",
        to_alg="slh-dsa-sha2-256s",
        renewal_timestamp_ref=d256("t2"),
        resealed_at="2033-01-01T00:00:00Z",
    )
    with pytest.raises(BrokenResealRecordError) as exc:
        append_crypto_migration(f2, bad)
    assert "renewal_timestamp_reused" in _codes(exc.value.verification)


def test_append_refuses_already_broken_reseal_record() -> None:
    data = _load("valid-anchor.json")
    data[CRYPTO_MIGRATION_FIELD] = [_event(to_alg="ed25519").model_dump()]
    with pytest.raises(BrokenResealRecordError, match="already fails"):
        append_crypto_migration(PreservationFacet.model_validate(data), _event())


# ── NF-334 model ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("covered_digest", "sha256:XYZ"),
        ("covered_digest", b"bytes"),
        ("new_timestamp_ref", "https://tsa.example.org/t"),
        ("parent", "not-a-digest"),
        ("new_hash_alg", "SHA256"),
        ("renewed_before", "2030-13-01"),
        ("renewed_before", "2030-01-01T00:00:00"),
        ("renewed_at", "yesterday"),
        ("timestamp_expires_at", "later"),
        ("renewal_type", "resign"),
    ],
)
def test_renewal_shape_errors(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _renewal(**{field: value})


def test_hash_strength_order_is_explicit() -> None:
    assert HASH_STRENGTH["sha256"] == HASH_STRENGTH["sha3-256"] < HASH_STRENGTH["sha512"]
    assert "sha1" not in HASH_STRENGTH and "md5" not in HASH_STRENGTH


# ── NF-334 verification ───────────────────────────────────────────────────


def _two(**second: Any) -> list[LtvRenewal]:
    first = _renewal(timestamp_expires_at="2035-01-01")
    base: dict[str, Any] = {
        "renewal_type": "timestamp_renewal",
        "covered_digest": first.new_timestamp_ref,
        "new_timestamp_ref": d256("tst-b"),
        "new_hash_alg": "sha256",
        "renewed_before": "2034-01-01",
        "parent": first.new_timestamp_ref,
    }
    base.update(second)
    return [first, _renewal(**base)]


def test_two_timestamp_renewals_ok() -> None:
    assert verify_ltv_chain(_two()).ok


def test_timestamp_renewal_must_cover_previous_token() -> None:
    v = verify_ltv_chain(_two(covered_digest=d256("other")))
    assert _codes(v) == {"covered_digest_mismatch"} and not v.covers_previous and v.hash_algs_ok


def test_first_renewal_with_parent_and_missing_parent() -> None:
    assert _codes(verify_ltv_chain([_renewal(parent=d256("ghost"))])) == {
        "first_renewal_has_parent"
    }
    assert "missing_parent" in _codes(verify_ltv_chain(_two(parent=None)))


def test_hash_tree_renewal_rules() -> None:
    ok = _two(
        renewal_type="hash_tree_renewal",
        covered_digest=d512("state"),
        new_timestamp_ref=d512("tok"),
        new_hash_alg="sha512",
    )
    assert verify_ltv_chain(ok).ok
    mismatch = _two(
        renewal_type="hash_tree_renewal", covered_digest=d256("s"), new_hash_alg="sha512"
    )
    assert "covered_alg_mismatch" in _codes(verify_ltv_chain(mismatch))
    reused = _two(renewal_type="hash_tree_renewal", covered_digest=d256("evidence"))
    assert "covered_digest_reused" in _codes(verify_ltv_chain(reused))


def test_timestamp_renewal_cannot_change_hash() -> None:
    v = verify_ltv_chain(_two(new_hash_alg="sha3-256", new_timestamp_ref=d3("t")))
    assert _codes(v) == {"hash_alg_changed_by_timestamp_renewal"}


def test_downgrade_unknown_and_length_findings() -> None:
    down = _two(
        renewal_type="hash_tree_renewal",
        new_hash_alg="sha224",
        covered_digest="sha224:" + hashlib.sha224(b"x").hexdigest(),
    )
    assert "hash_alg_downgrade" in _codes(verify_ltv_chain(down))
    unknown = verify_ltv_chain([_renewal(new_hash_alg="sha1", covered_digest="sha1:" + "a" * 40)])
    assert _codes(unknown) == {"unknown_hash_alg"} and not unknown.hash_algs_ok
    short = verify_ltv_chain([_renewal(covered_digest="sha512:" + "a" * 64)])
    assert _codes(short) == {"digest_length_mismatch"}


def test_timestamp_ref_loop() -> None:
    v = verify_ltv_chain([_renewal(new_timestamp_ref=d256("evidence"))])
    assert _codes(v) == {"timestamp_ref_reused"}


def test_timing_findings() -> None:
    assert "renewed_before_not_monotonic" in _codes(
        verify_ltv_chain(_two(renewed_before="2029-01-01"))
    )
    late = verify_ltv_chain(_two(renewed_before="2036-01-01"))
    assert _codes(late) == {"renewed_after_prior_expiry"} and not late.renewed_in_time
    late_at = verify_ltv_chain(_two(renewed_at="2035-06-01T00:00:00Z", renewed_before="2034-01-01"))
    assert {"renewed_after_prior_expiry", "renewed_at_not_before_deadline"} <= _codes(late_at)
    rfc3339 = verify_ltv_chain(
        [_renewal(renewed_before="2030-01-01T00:00:00Z", renewed_at="2029-12-31T23:59:59+00:00")]
    )
    assert rfc3339.ok


def test_ltv_too_many(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reseal_mod, "MAX_RECORDS", 1)
    v = verify_ltv_chain(_two())
    assert _codes(v) == {"too_many_records"} and not v.ok


def test_plan_and_append_ltv() -> None:
    facet = _facet()
    with pytest.raises(PreservationError, match="covered_digest is required"):
        plan_ltv_renewal(
            facet,
            renewal_type="timestamp_renewal",
            new_timestamp_ref=d256("a"),
            new_hash_alg="sha256",
            renewed_before="2030-01-01",
            renewed_at="2029-01-01T00:00:00Z",
        )
    r1 = plan_ltv_renewal(
        facet,
        renewal_type="timestamp_renewal",
        covered_digest=d256("ev"),
        new_timestamp_ref=d256("a"),
        new_hash_alg="sha256",
        renewed_before="2030-01-01",
        renewed_at="2029-01-01T00:00:00Z",
    )
    f1 = append_ltv_renewal(facet, r1)
    raw = (f1.model_extra or {})[LTV_CHAIN_FIELD]
    assert raw[0]["parent"] is None and "timestamp_expires_at" not in raw[0]
    assert f1.provenance_events[-1].event == "timestamp renewal"
    r2 = plan_ltv_renewal(
        f1,
        renewal_type="timestamp_renewal",
        new_timestamp_ref=d256("b"),
        new_hash_alg="sha256",
        renewed_before="2031-01-01",
        renewed_at="2030-06-01T00:00:00Z",
    )
    assert r2.covered_digest == r2.parent == d256("a")
    f2 = append_ltv_renewal(f1, r2, record_event=False)
    verify_p3_append_only(f1, f2)
    bad = _renewal(
        renewal_type="hash_tree_renewal",
        covered_digest=d256("x"),
        new_hash_alg="sha224",
        new_timestamp_ref=d256("c"),
        parent=d256("b"),
        renewed_before="2032-01-01",
    )
    with pytest.raises(BrokenLtvChainError) as exc:
        append_ltv_renewal(f2, bad)
    assert "hash_alg_downgrade" in _codes(exc.value.verification)


def test_append_refuses_already_broken_ltv_chain() -> None:
    with pytest.raises(BrokenLtvChainError, match="already fails"):
        append_ltv_renewal(_facet("invalid-ltv-chain-downgrade.json"), _renewal())


# ── Append-only guard ─────────────────────────────────────────────────────


def test_p3_append_only_guard() -> None:
    full = _facet("valid-reseal-ltv-chain.json")
    data = _load("valid-reseal-ltv-chain.json")
    data[LTV_CHAIN_FIELD] = data[LTV_CHAIN_FIELD][:1]
    shorter = PreservationFacet.model_validate(data)
    verify_p3_append_only(shorter, full)
    with pytest.raises(P3RewriteError, match="shrank"):
        verify_p3_append_only(full, shorter)
    data = _load("valid-reseal-ltv-chain.json")
    data[CRYPTO_MIGRATION_FIELD][0]["to_alg"] = "ml-dsa-87"
    with pytest.raises(P3RewriteError, match="rewritten"):
        verify_p3_append_only(full, PreservationFacet.model_validate(data))
    data = _load("valid-reseal-ltv-chain.json")
    data["original_root"] = d256("other-root")
    with pytest.raises(P3RewriteError, match="original_root"):
        verify_p3_append_only(full, PreservationFacet.model_validate(data))


def test_module_is_record_only() -> None:
    """No key generation, no network, no PQC library (I-2 / I-4)."""
    src = Path(reseal_mod.__file__).read_text()
    for banned in ("import socket", "urllib", "requests", "httpx", "generate_private_key", "oqs"):
        assert banned not in src
