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

"""ADR-0150 P3 — ISO/IEC TS 27560-shaped consent receipt (NF-183)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.hitl import (
    ConsentReceipt,
    ConsentReceiptError,
    IdentityRefError,
    RecordContentError,
    build_consent_receipt,
    compute_receipt_digest,
    load_consents,
    record_consent,
    verify_consents,
    withdraw_consent,
)
from novafabric.hitl._records import MAX_RECORDS_PER_KIND, normalise_key
from novafabric.hitl.consent import CONSENT_NOTICE, MAX_ACTIONS

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "conversation"
SCHEMA = REPO_ROOT / "schemas" / "run-capsule.schema.json"
P3 = FIXTURES / "p3-accountability-capsule.json"
TAMPERED = FIXTURES / "invalid-tampered-consent-capsule.json"
THREADED = FIXTURES / "threaded-capsule.json"
TEXT_ONLY = FIXTURES / "valid-text-only-capsule.json"


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _receipt(**kw: Any) -> ConsentReceipt:
    base: dict[str, Any] = {
        "consent_id": "c-1",
        "subject_ref": "human:did:example:alice",
        "purpose": "dpv:ServiceProvision",
        "action": ["dpv:Store"],
        "given_at": "2026-07-15T10:00:00Z",
    }
    base.update(kw)
    return build_consent_receipt(**base)


# ── Golden fixtures ───────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [P3, TAMPERED])
def test_fixtures_are_schema_valid(path: Path) -> None:
    jsonschema.validate(_load(path), json.loads(SCHEMA.read_text()))


def test_golden_consent_verifies_ok() -> None:
    result = verify_consents(_load(P3))
    assert result.status == "ok"
    assert [v.consent_id for v in result.verdicts] == ["consent-0001"]
    assert result.verdicts[0].to_dict()["ok"] is True


def test_tampered_consent_is_defective() -> None:
    result = verify_consents(_load(TAMPERED))
    assert result.status == "defective"
    assert result.verdicts[0].digest_matches is False


def test_iso_27560_shape_fields_present() -> None:
    stored = _load(P3)["facets"]["conversation"]["consent"][0]
    for key in (
        "consent_id",
        "subject_ref",
        "purpose",
        "action",
        "given_at",
        "expiry",
        "withdrawable",
        "receipt_digest",
    ):
        assert key in stored


def test_notice_disclaims_legal_validity() -> None:
    assert "does not assert" in CONSENT_NOTICE
    assert "legally valid" in CONSENT_NOTICE


# ── Build + digest ────────────────────────────────────────────────────────


def test_build_binds_digest_and_is_deterministic() -> None:
    a = _receipt()
    b = _receipt()
    assert a.receipt_digest == b.receipt_digest
    assert a.receipt_digest == compute_receipt_digest(a.model_dump(exclude_none=True))
    assert a.withdrawable is True


def test_digest_covers_extension_fields() -> None:
    a = _receipt(controller_ref="org:acme")
    b = _receipt(controller_ref="org:other")
    assert a.receipt_digest != b.receipt_digest


def test_digest_excludes_withdrawal() -> None:
    rec = _receipt()
    withdrawn = withdraw_consent(rec, withdrawn_at="2026-08-01T00:00:00Z")
    assert withdrawn.receipt_digest == rec.receipt_digest
    assert withdrawn.withdrawn_at == "2026-08-01T00:00:00Z"


def test_withdraw_twice_refused() -> None:
    rec = withdraw_consent(_receipt(), withdrawn_at="2026-08-01T00:00:00Z")
    with pytest.raises(ConsentReceiptError, match="already withdrawn"):
        withdraw_consent(rec, withdrawn_at="2026-09-01T00:00:00Z")


def test_withdraw_not_withdrawable_refused() -> None:
    with pytest.raises(ConsentReceiptError, match="non-withdrawable"):
        withdraw_consent(_receipt(withdrawable=False), withdrawn_at="2026-08-01T00:00:00Z")


def test_withdraw_before_given_refused() -> None:
    with pytest.raises(ConsentReceiptError, match="earlier than given_at"):
        withdraw_consent(_receipt(), withdrawn_at="2026-01-01T00:00:00Z")


def test_expiry_before_given_refused() -> None:
    with pytest.raises(ConsentReceiptError, match="expiry"):
        _receipt(expiry="2026-07-15T09:00:00Z")


# ── Field validation (D7) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("override", "exc"),
    [
        ({"subject_ref": "alice"}, IdentityRefError),
        ({"subject_ref": "human:alice@example.com"}, IdentityRefError),
        ({"subject_ref": "agent:spiffe://x/y"}, IdentityRefError),
        ({"purpose": "provide the service to alice"}, RecordContentError),
        ({"purpose": "x" * 300}, RecordContentError),
        ({"action": "dpv:Store"}, ConsentReceiptError),
        ({"action": []}, ConsentReceiptError),
        ({"action": ["dpv:Store", "dpv:Store"]}, ConsentReceiptError),
        ({"action": [f"dpv:A{i}" for i in range(MAX_ACTIONS + 1)]}, ConsentReceiptError),
        ({"consent_id": "has space"}, RecordContentError),
        ({"consent_id": "c" * 200}, RecordContentError),
        ({"withdrawable": "false"}, ConsentReceiptError),
        ({"given_at": "2026-07-15T10:00:00Z" + "0" * 80}, Exception),
        ({"turn_ref": "a sentence of prose"}, RecordContentError),
        ({"prompt_text": "hi"}, RecordContentError),
        ({"Consent-Body": "hi"}, RecordContentError),
        ({"note": {"nested": 1}}, RecordContentError),
    ],
)
def test_invalid_fields_refused(override: dict[str, Any], exc: type[Exception]) -> None:
    with pytest.raises(exc):
        _receipt(**override)


def test_normalise_key_folds_separators_and_width() -> None:
    assert normalise_key("Reason-Text") == "reasontext"
    assert normalise_key("ＲＥＡＳＯＮ．ＴＥＸＴ") == "reasontext"


def test_stored_digest_mismatch_detected() -> None:
    rec = _receipt()
    forged = ConsentReceipt.model_validate(
        {**rec.model_dump(exclude_none=True), "purpose": "dpv:Marketing"}
    )
    out = record_consent(_load(THREADED), forged)
    assert not out.recorded
    assert "receipt_digest" in (out.reason or "")


# ── Recording (fail-open) ─────────────────────────────────────────────────


def test_record_without_conversation_creates_block() -> None:
    capsule = _load(TEXT_ONLY)
    assert "conversation" not in (capsule.get("facets") or {})
    out = record_consent(capsule, _receipt())
    assert out.recorded
    block = out.capsule["facets"]["conversation"]
    assert block["schema_version"] == "0.1.0"
    assert len(block["consent"]) == 1
    assert "facets" not in capsule or "conversation" not in capsule["facets"]
    jsonschema.validate(out.capsule, json.loads(SCHEMA.read_text()))


def test_record_with_turn_ref_must_resolve() -> None:
    capsule = _load(THREADED)
    assert record_consent(capsule, _receipt(turn_ref="t0")).recorded
    out = record_consent(capsule, _receipt(turn_ref="t99"))
    assert not out.recorded and out.capsule is capsule


def test_turn_ref_without_conversation_not_recorded() -> None:
    out = record_consent(_load(TEXT_ONLY), _receipt(turn_ref="t0"))
    assert not out.recorded


def test_duplicate_consent_id_not_recorded() -> None:
    first = record_consent(_load(THREADED), _receipt())
    second = record_consent(first.capsule, _receipt(purpose="dpv:Marketing"))
    assert not second.recorded
    assert second.reason == "consent_id already recorded"


def test_record_malformed_mapping_never_raises() -> None:
    capsule = _load(THREADED)
    out = record_consent(capsule, {"consent_id": "c", "subject_ref": "alice@example.com"})
    assert not out.recorded and out.capsule is capsule


def test_record_existing_not_a_list() -> None:
    capsule = _load(THREADED)
    capsule["facets"]["conversation"]["consent"] = {"oops": 1}
    assert not record_consent(capsule, _receipt()).recorded


def test_record_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    capsule = _load(THREADED)
    capsule["facets"]["conversation"]["consent"] = [{}] * MAX_RECORDS_PER_KIND
    out = record_consent(capsule, _receipt())
    assert not out.recorded and out.reason == "consent record cap reached"


# ── Verification (fail-closed) ────────────────────────────────────────────


def test_verify_empty() -> None:
    assert verify_consents(_load(THREADED)).status == "empty"


def test_verify_flags_duplicates_dangling_and_malformed() -> None:
    capsule = _load(P3)
    block = capsule["facets"]["conversation"]
    good = block["consent"][0]
    dangling = _receipt(consent_id="c-2", turn_ref="t404").model_dump(exclude_none=True)
    block["consent"] = [good, copy.deepcopy(good), dangling, {"consent_id": 5}]
    result = verify_consents(capsule)
    assert result.status == "defective"
    by_index = {v.index: v for v in result.verdicts}
    assert by_index[0].duplicate_id and by_index[1].duplicate_id
    assert by_index[2].turn_resolves is False
    assert [d.index for d in result.defects] == [3]


def test_verify_with_malformed_thread_resolves_nothing() -> None:
    capsule = _load(P3)
    capsule["facets"]["conversation"]["turns"] = "not a list"
    result = verify_consents(capsule)
    assert result.verdicts[0].turn_resolves is False


def test_withdrawn_receipt_still_ok() -> None:
    capsule = _load(THREADED)
    rec = withdraw_consent(_receipt(), withdrawn_at="2026-08-01T00:00:00Z")
    out = record_consent(capsule, rec)
    result = verify_consents(out.capsule)
    assert result.status == "ok"
    assert result.verdicts[0].withdrawn is True


def test_load_consents_non_list_is_defect() -> None:
    capsule = _load(THREADED)
    capsule["facets"]["conversation"]["consent"] = "x"
    loaded = load_consents(capsule)
    assert loaded.defects and loaded.defects[0].index == -1
