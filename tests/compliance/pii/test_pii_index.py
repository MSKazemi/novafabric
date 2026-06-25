# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for RedactionSubjectIndex (cap-001)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from novafabric.compliance.pii.index import RedactionSubjectIndex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestRedactionSubjectIndex:
    def test_open_creates_schema(self, tmp_path: Path) -> None:
        db = tmp_path / "idx.db"
        with RedactionSubjectIndex(db_path=db) as idx:
            assert idx.count() == 0

    def test_record_and_lookup_by_hmac(self, tmp_path: Path) -> None:
        db = tmp_path / "idx.db"
        hmac_val = "sha256:" + "a" * 64
        with RedactionSubjectIndex(db_path=db) as idx:
            idx.record(
                subject_id_hmac=hmac_val,
                capsule_id="CAP-001",
                field_path=".email",
                legal_basis="operator_declared_compliance",
                redacted_at_utc=_now(),
            )
            records = idx.lookup_subject(hmac_val)
        assert len(records) == 1
        assert records[0].capsule_id == "CAP-001"
        assert records[0].field_path == ".email"

    def test_multi_capsule_same_subject(self, tmp_path: Path) -> None:
        db = tmp_path / "idx.db"
        hmac_val = "sha256:" + "b" * 64
        with RedactionSubjectIndex(db_path=db) as idx:
            idx.record(hmac_val, "CAP-001", ".email", "gdpr_art17", _now())
            idx.record(hmac_val, "CAP-002", ".name", "gdpr_art17", _now())
            idx.record(hmac_val, "CAP-003", ".phone", "gdpr_art17", _now())
            records = idx.lookup_subject(hmac_val)
        assert len(records) == 3
        capsule_ids = {r.capsule_id for r in records}
        assert capsule_ids == {"CAP-001", "CAP-002", "CAP-003"}

    def test_duplicate_primary_key_ignored(self, tmp_path: Path) -> None:
        db = tmp_path / "idx.db"
        hmac_val = "sha256:" + "c" * 64
        with RedactionSubjectIndex(db_path=db) as idx:
            idx.record(hmac_val, "CAP-001", ".email", "gdpr", _now())
            idx.record(hmac_val, "CAP-001", ".email", "gdpr", _now())  # duplicate
            assert idx.count() == 1

    def test_lookup_capsule(self, tmp_path: Path) -> None:
        db = tmp_path / "idx.db"
        with RedactionSubjectIndex(db_path=db) as idx:
            idx.record("sha256:" + "d" * 64, "CAP-X", ".a", "gdpr", _now())
            idx.record("sha256:" + "e" * 64, "CAP-X", ".b", "gdpr", _now())
            idx.record("sha256:" + "f" * 64, "CAP-Y", ".c", "gdpr", _now())
            x_recs = idx.lookup_capsule("CAP-X")
            y_recs = idx.lookup_capsule("CAP-Y")
        assert len(x_recs) == 2
        assert len(y_recs) == 1

    def test_unknown_hmac_returns_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "idx.db"
        with RedactionSubjectIndex(db_path=db) as idx:
            records = idx.lookup_subject("sha256:" + "0" * 64)
        assert records == []

    def test_not_open_raises(self, tmp_path: Path) -> None:
        db = tmp_path / "idx.db"
        idx = RedactionSubjectIndex(db_path=db)
        with pytest.raises(RuntimeError):
            idx.count()

    def test_record_not_open_raises(self, tmp_path: Path) -> None:
        idx = RedactionSubjectIndex(db_path=tmp_path / "idx.db")
        with pytest.raises(RuntimeError, match="not open"):
            idx.record("sha256:" + "a" * 64, "CAP-001", ".email", "gdpr", "2026-01-01T00:00:00Z")

    def test_lookup_subject_not_open_raises(self, tmp_path: Path) -> None:
        idx = RedactionSubjectIndex(db_path=tmp_path / "idx.db")
        with pytest.raises(RuntimeError, match="not open"):
            idx.lookup_subject("sha256:" + "a" * 64)

    def test_lookup_capsule_not_open_raises(self, tmp_path: Path) -> None:
        idx = RedactionSubjectIndex(db_path=tmp_path / "idx.db")
        with pytest.raises(RuntimeError, match="not open"):
            idx.lookup_capsule("CAP-001")
