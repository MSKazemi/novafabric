"""The ISO 42001 examiner export covers the period asked for (review serve-16).

``POST /api/compliance/examiner/iso42001`` always used ``period_start =
2026-01-01`` — a constant that silently skews every package as time goes on.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.serve.app import create_app

TOKEN = "iso-period-token"
HEADERS = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def periods(monkeypatch: pytest.MonkeyPatch) -> list[tuple[datetime, datetime]]:
    seen: list[tuple[datetime, datetime]] = []
    from novafabric.compliance.export import examiner

    real = examiner.ISO42001Exporter

    class _Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **kw: Any) -> None:
            seen.append((kw["period_start"], kw["period_end"]))
            super().__init__(*a, **kw)

    monkeypatch.setattr(examiner, "ISO42001Exporter", _Recording)
    return seen


@pytest.fixture()
def client_and_run(tmp_path: Path) -> tuple[TestClient, str]:
    store = tmp_path / "runs"
    store.mkdir()
    cap = CaptureOrchestrator(base_dir=store).run(command=[sys.executable, "-c", "pass"]).capsule_dir
    return TestClient(create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "r.db")), cap.name


def test_the_default_period_is_the_trailing_year(
    client_and_run: tuple[TestClient, str], periods: list[tuple[datetime, datetime]]
) -> None:
    client, run_id = client_and_run
    resp = client.post("/api/compliance/examiner/iso42001", headers=HEADERS, json={"run_id": run_id})
    assert resp.status_code == 200, resp.text
    ((start, end),) = periods
    assert abs((end - start) - timedelta(days=365)) < timedelta(minutes=1)
    assert abs(datetime.now(timezone.utc) - end) < timedelta(minutes=1)
    assert resp.json()["period"] == f"{start.date()}/{end.date()}"


def test_an_explicit_period_is_used(
    client_and_run: tuple[TestClient, str], periods: list[tuple[datetime, datetime]]
) -> None:
    client, run_id = client_and_run
    resp = client.post("/api/compliance/examiner/iso42001", headers=HEADERS,
                       json={"run_id": run_id, "period": "2026-01-01/2026-06-30"})
    assert resp.status_code == 200, resp.text
    ((start, end),) = periods
    assert (start.date().isoformat(), end.date().isoformat()) == ("2026-01-01", "2026-06-30")


@pytest.mark.parametrize("period", ["2026", "2026-07-01/2026-01-01", "x/y"])
def test_an_invalid_period_is_422(
    client_and_run: tuple[TestClient, str], periods: list[tuple[datetime, datetime]], period: str
) -> None:
    client, run_id = client_and_run
    resp = client.post("/api/compliance/examiner/iso42001", headers=HEADERS,
                       json={"run_id": run_id, "period": period})
    assert resp.status_code == 422, resp.text
    assert periods == []
