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
"""Tests for the read-only PII status report builder (ADR-0069, `nova pii status`).

Covers: active-DEK correlation via NOVA_PII_PEPPER, erased subjects, missing
capsule, missing DEK store, missing pepper (unknown state), capsule without a
redaction manifest, resolution by capsule ID, and invalid manifest JSON.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from novafabric.pii.dek import DEKStore
from novafabric.pii.status import (
    CapsuleNotFoundError,
    ManifestInvalidError,
    PIIStatusReport,
    build_pii_status,
)

PEPPER = b"test-pepper"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hmac(subject_id: str, pepper: bytes = PEPPER) -> str:
    """Compute the subject HMAC exactly as PIIDetectionGate records it."""
    return "sha256:" + hmac.new(pepper, subject_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _write_manifest(
    capsule_dir: Path,
    capsule_id: str,
    subject_ids: list[str],
    rule_id: str = "EMAIL",
) -> Path:
    """Write a minimal redaction_manifest.json with one entry per subject."""
    capsule_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "field_path": f"model_calls[{i}].messages[0].content",
            "detection_rule_id": rule_id,
            "legal_basis": "GDPR Art.17",
            "subject_id_hmac": _hmac(subject),
            "redacted_at_utc": f"2026-01-0{i + 1}T00:00:00+00:00",
        }
        for i, subject in enumerate(subject_ids)
    ]
    manifest_path = capsule_dir / "redaction_manifest.json"
    manifest_path.write_text(
        json.dumps({"capsule_id": capsule_id, "entries": entries}),
        encoding="utf-8",
    )
    return manifest_path


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated NOVAFABRIC_HOME with the pepper set."""
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    monkeypatch.setenv("NOVA_PII_PEPPER", PEPPER.decode("utf-8"))
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: active DEK — subject correlates to "active"
# ---------------------------------------------------------------------------


def test_active_subject_reported(home: Path) -> None:
    """A subject with a live DEK is reported as dek_state='active' with created_at."""
    subject = "alice@example.com"
    store = DEKStore(home / "dek.db")
    dek = store.get_or_create_dek(subject)
    store.close()

    capsule_dir = home / "capsules" / "01HXCAPS"
    _write_manifest(capsule_dir, "01HXCAPS", [subject])

    report = build_pii_status(str(capsule_dir))

    assert isinstance(report, PIIStatusReport)
    assert report.capsule_id == "01HXCAPS"
    assert report.manifest_present is True
    assert report.dek_store_present is True
    assert report.pepper_available is True
    assert report.encrypted_field_count == 1
    assert len(report.subjects) == 1
    subj = report.subjects[0]
    assert subj.subject_id_hmac == _hmac(subject)
    assert subj.dek_state == "active"
    assert subj.dek_created_at == dek.created_at
    assert subj.tenant_id == "default"
    assert subj.field_count == 1
    assert subj.detection_rule_ids == ["EMAIL"]


# ---------------------------------------------------------------------------
# Test 2: erased subject — DEK destroyed -> "erased"
# ---------------------------------------------------------------------------


def test_erased_subject_reported(home: Path) -> None:
    """After crypto-shredding, the subject is reported as dek_state='erased'."""
    subject = "erased@example.com"
    store = DEKStore(home / "dek.db")
    store.get_or_create_dek(subject)
    store.erase_subject(subject, capsule_ids=["01HXERAS"], retention_months=0)
    store.close()

    capsule_dir = home / "capsules" / "01HXERAS"
    _write_manifest(capsule_dir, "01HXERAS", [subject])

    report = build_pii_status(str(capsule_dir))

    assert report.subjects[0].dek_state == "erased"
    assert report.subjects[0].dek_created_at is None


# ---------------------------------------------------------------------------
# Test 3: mixed subjects — one active, one erased
# ---------------------------------------------------------------------------


def test_mixed_active_and_erased_subjects(home: Path) -> None:
    """Active and erased subjects are distinguished within one capsule."""
    store = DEKStore(home / "dek.db")
    store.get_or_create_dek("keep@example.com")
    store.get_or_create_dek("gone@example.com")
    store.erase_subject("gone@example.com", capsule_ids=[], retention_months=0)
    store.close()

    capsule_dir = home / "capsules" / "01HXMIXD"
    _write_manifest(capsule_dir, "01HXMIXD", ["keep@example.com", "gone@example.com"])

    report = build_pii_status(str(capsule_dir))

    states = {s.subject_id_hmac: s.dek_state for s in report.subjects}
    assert states[_hmac("keep@example.com")] == "active"
    assert states[_hmac("gone@example.com")] == "erased"
    assert report.encrypted_field_count == 2


# ---------------------------------------------------------------------------
# Test 4: missing capsule raises CapsuleNotFoundError
# ---------------------------------------------------------------------------


def test_missing_capsule_raises(home: Path) -> None:
    """An unknown capsule ID (and path) raises CapsuleNotFoundError."""
    with pytest.raises(CapsuleNotFoundError):
        build_pii_status("01HXDOESNOTEXIST")


# ---------------------------------------------------------------------------
# Test 5: no DEK store — reported absent; subjects effectively erased
# ---------------------------------------------------------------------------


def test_no_dek_store_reports_absent_and_does_not_create_it(home: Path) -> None:
    """Without dek.db the store is reported absent, subjects are 'erased',
    and — critically — the read-only status call never creates dek.db."""
    capsule_dir = home / "capsules" / "01HXNODB"
    _write_manifest(capsule_dir, "01HXNODB", ["bob@example.com"])

    report = build_pii_status(str(capsule_dir))

    assert report.dek_store_present is False
    assert report.subjects[0].dek_state == "erased"
    assert not (home / "dek.db").exists(), "status must never create dek.db"


# ---------------------------------------------------------------------------
# Test 6: no pepper — correlation impossible -> "unknown"
# ---------------------------------------------------------------------------


def test_no_pepper_reports_unknown(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With dek.db present but NOVA_PII_PEPPER unset, dek_state is 'unknown'."""
    monkeypatch.delenv("NOVA_PII_PEPPER")
    store = DEKStore(home / "dek.db")
    store.get_or_create_dek("carol@example.com")
    store.close()

    capsule_dir = home / "capsules" / "01HXNOPP"
    _write_manifest(capsule_dir, "01HXNOPP", ["carol@example.com"])

    report = build_pii_status(str(capsule_dir))

    assert report.pepper_available is False
    assert report.dek_store_present is True
    assert report.subjects[0].dek_state == "unknown"


# ---------------------------------------------------------------------------
# Test 7: capsule dir without a manifest — zero fields, no error
# ---------------------------------------------------------------------------


def test_capsule_without_manifest(home: Path) -> None:
    """A capsule directory without redaction_manifest.json reports zero PII fields."""
    capsule_dir = home / "capsules" / "01HXCLEAN"
    capsule_dir.mkdir(parents=True)

    report = build_pii_status(str(capsule_dir))

    assert report.capsule_id == "01HXCLEAN"
    assert report.manifest_present is False
    assert report.encrypted_field_count == 0
    assert report.subjects == []
    assert report.fields == []


# ---------------------------------------------------------------------------
# Test 8: resolution by capsule ID scans the capsule directory
# ---------------------------------------------------------------------------


def test_resolve_by_capsule_id(home: Path) -> None:
    """A bare capsule ID is resolved by scanning $NOVAFABRIC_HOME/capsules/."""
    subject = "dave@example.com"
    store = DEKStore(home / "dek.db")
    store.get_or_create_dek(subject)
    store.close()

    capsule_dir = home / "capsules" / "some-run-dir"
    _write_manifest(capsule_dir, "01HXBYID", [subject])

    report = build_pii_status("01HXBYID")

    assert report.capsule_id == "01HXBYID"
    assert report.manifest_present is True
    assert report.subjects[0].dek_state == "active"


# ---------------------------------------------------------------------------
# Test 9: invalid manifest JSON raises ManifestInvalidError
# ---------------------------------------------------------------------------


def test_invalid_manifest_raises(home: Path) -> None:
    """A corrupt redaction_manifest.json raises ManifestInvalidError."""
    capsule_dir = home / "capsules" / "01HXBAD"
    capsule_dir.mkdir(parents=True)
    (capsule_dir / "redaction_manifest.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ManifestInvalidError):
        build_pii_status(str(capsule_dir))


# ---------------------------------------------------------------------------
# Test 9b: ID resolves directly when capsule_dir/<id>/ is a directory
# ---------------------------------------------------------------------------


def test_resolve_by_id_direct_directory(home: Path) -> None:
    """capsule_dir/<capsule_id>/ resolves without a recursive scan."""
    _write_manifest(home / "capsules" / "01HXNEST", "01HXNEST", ["grace@example.com"])

    report = build_pii_status("01HXNEST")

    assert report.capsule_id == "01HXNEST"
    assert report.capsule_dir == str(home / "capsules" / "01HXNEST")


# ---------------------------------------------------------------------------
# Test 10: ID scan skips unreadable manifests and still finds the match
# ---------------------------------------------------------------------------


def test_resolve_by_id_skips_corrupt_manifests(home: Path) -> None:
    """A corrupt manifest elsewhere in capsule_dir does not break ID resolution."""
    bad_dir = home / "capsules" / "corrupt-run"
    bad_dir.mkdir(parents=True)
    (bad_dir / "redaction_manifest.json").write_text("{not json", encoding="utf-8")
    _write_manifest(home / "capsules" / "good-run", "01HXGOOD", ["frank@example.com"])

    report = build_pii_status("01HXGOOD")

    assert report.capsule_id == "01HXGOOD"
    assert report.manifest_present is True


# ---------------------------------------------------------------------------
# Test 11: report JSON round-trips
# ---------------------------------------------------------------------------


def test_report_json_round_trip(home: Path) -> None:
    """The report serialises to JSON and validates back."""
    capsule_dir = home / "capsules" / "01HXJSON"
    _write_manifest(capsule_dir, "01HXJSON", ["eve@example.com"])

    report = build_pii_status(str(capsule_dir))
    payload = json.loads(report.model_dump_json())
    restored = PIIStatusReport.model_validate(payload)

    assert restored.capsule_id == "01HXJSON"
    assert restored.encrypted_field_count == 1
