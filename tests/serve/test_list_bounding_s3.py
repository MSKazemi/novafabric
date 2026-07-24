"""S3 scale slice — bounded evidence + incident list endpoints (ADR-0199).

Both ``GET /api/evidence`` and ``GET /api/incidents`` used to materialise every
record on disk on every call (evidence opened and hashed every zip; incidents
parsed every row). This suite pins the new bound: the response carries at most
``limit`` items (default-capped), reports the true ``total``, and flags
``truncated`` when older records were not returned. Also unit-tests the pushed-
down ``IncidentStore.list_recent`` / ``count``.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.compliance.incident.models import Incident, IncidentSeverity  # noqa: E402
from novafabric.compliance.incident.store import IncidentStore  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"


def _write_bundle(evidence_dir: Path, run_id: str) -> None:
    zip_path = evidence_dir / f"{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"run_id": run_id, "created_at": "2026-07-24"}))


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    runs = tmp_path / "runs"
    runs.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("NOVAFABRIC_INCIDENTS_DB_PATH", str(tmp_path / "incidents.db"))
    app = create_app(
        token=VALID_TOKEN, capsule_dir=runs, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        c._evidence_dir = evidence  # type: ignore[attr-defined]
        yield c


class TestEvidenceBounding:
    def test_bounded_with_total_and_truncated(self, client: TestClient) -> None:
        evidence = client._evidence_dir  # type: ignore[attr-defined]
        for i in range(7):
            _write_bundle(evidence, f"run{i:03d}")
        res = client.get(f"/api/evidence?limit=3&{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 3
        assert body["total"] == 7
        assert body["truncated"] is True

    def test_not_truncated_when_within_limit(self, client: TestClient) -> None:
        evidence = client._evidence_dir  # type: ignore[attr-defined]
        _write_bundle(evidence, "solo")
        res = client.get(f"/api/evidence?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 1
        assert body["total"] == 1
        assert body["truncated"] is False

    def test_limit_over_cap_rejected(self, client: TestClient) -> None:
        res = client.get(f"/api/evidence?limit=999999&{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 422  # exceeds the ge/le Query bound


class TestIncidentBounding:
    def _seed(self, tmp_db: Path, n: int) -> None:
        with IncidentStore(db_path=tmp_db) as store:
            for i in range(n):
                store.create(
                    Incident(
                        title=f"incident {i}",
                        classification="unauthorized_tool_use",
                        severity=IncidentSeverity.HIGH,
                        occurred_at=datetime(2026, 6, 10, 8, i % 60, tzinfo=timezone.utc),
                    )
                )

    def test_bounded_response(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        self._seed(tmp_path / "incidents.db", 6)
        res = client.get(f"/api/incidents?limit=2&{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["count"] == 2
        assert body["total"] == 6
        assert body["truncated"] is True


class TestIncidentStoreMethods:
    def test_count_and_list_recent(self, tmp_path: Path) -> None:
        db = tmp_path / "incidents.db"
        with IncidentStore(db_path=db) as store:
            for i in range(4):
                store.create(
                    Incident(
                        title=f"i{i}",
                        classification="unauthorized_tool_use",
                        severity=IncidentSeverity.LOW,
                        occurred_at=datetime(2026, 6, 10, 8, i, tzinfo=timezone.utc),
                    )
                )
            assert store.count() == 4
            recent = store.list_recent(2)
            assert len(recent) == 2
            # newest occurrence first
            assert recent[0].occurred_at >= recent[1].occurred_at
