"""Tests for the forensics-timeline dashboard read surface (ADR-0155 D1, ADR-0183 pattern).

``GET /api/runs/{run_id}/forensics-timeline`` reconstructs a deterministic
:class:`~novafabric.forensics.timeline.ForensicsTimeline` from the run's own
sealed capsule — lifecycle (created/finished) plus ``model-calls.jsonl`` /
``tool-calls.jsonl`` entries carrying a ``started_at`` timestamp — via the
pure :func:`~novafabric.forensics.timeline.merge_timeline`. Nothing in the
capture path today assembles a fuller incident → session → lineage evidence
set (see the module docstring: that collector is "a documented follow-on
slice"), so lineage evidence is always reported as a gap, never fabricated.

See src/novafabric/serve/routers/forensics.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"
RUN_ID = "01FORENSICSTEST0000000001"


def _manifest(**overrides: object) -> dict:
    base = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.63.0",
        "run_id": RUN_ID,
        "created_at": "2026-07-24T10:00:00+00:00",
        "finished_at": "2026-07-24T10:00:05+00:00",
        "duration_ms": 5000,
        "command": ["python", "-c", "print(1)"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    base.update(overrides)
    return base


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    cdir = base / RUN_ID
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(_manifest()))
    (cdir / "model-calls.jsonl").write_text(
        "\n".join(
            json.dumps(c)
            for c in [
                {"call_id": "call_1", "started_at": "2026-07-24T10:00:01+00:00", "model": "gpt-4o-mini"},
                {"call_id": "call_2", "started_at": "2026-07-24T10:00:02+00:00", "model": "gpt-4o-mini"},
            ]
        )
        + "\n"
    )
    (cdir / "tool-calls.jsonl").write_text(
        json.dumps(
            {"call_id": "tc_1", "started_at": "2026-07-24T10:00:03+00:00", "tool_name": "read-file"}
        )
        + "\n"
    )
    return base


@pytest.fixture
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


class TestAuth:
    def test_requires_token(self, client: TestClient) -> None:
        res = client.get(f"/api/runs/{RUN_ID}/forensics-timeline", headers=HEADERS)
        assert res.status_code == 401


class TestHappyPath:
    def test_timeline_ordered_and_bounded(self, client: TestClient) -> None:
        res = client.get(f"/api/runs/{RUN_ID}/forensics-timeline?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        assert body["incident_id"] == RUN_ID
        kinds = [e["kind"] for e in body["events"]]
        assert kinds == ["run", "model-call", "model-call", "tool-call", "run"]
        # Deterministic (ts, source_capsule, seq) ordering.
        timestamps = [e["ts"] for e in body["events"]]
        assert timestamps == sorted(timestamps)
        assert body["honesty_line"]
        assert "does not establish causation" in body["honesty_line"]

    def test_lineage_gap_always_reported(self, client: TestClient) -> None:
        res = client.get(f"/api/runs/{RUN_ID}/forensics-timeline?{TOKEN_Q}", headers=HEADERS)
        gaps = res.json()["gaps"]
        assert any("lineage" in g for g in gaps)

    def test_missing_started_at_is_a_gap_not_a_crash(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        cdir = capsule_dir / RUN_ID
        (cdir / "model-calls.jsonl").write_text(
            json.dumps({"call_id": "no_ts", "model": "gpt-4o-mini"}) + "\n"
        )
        res = client.get(f"/api/runs/{RUN_ID}/forensics-timeline?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        assert not any(e["kind"] == "model-call" for e in body["events"])
        assert any("started_at" in g for g in body["gaps"])

    def test_capsule_without_finished_at(self, client: TestClient, capsule_dir: Path) -> None:
        cdir = capsule_dir / RUN_ID
        (cdir / "capsule.yaml").write_text(
            yaml.safe_dump(_manifest(finished_at=None, status="running"))
        )
        res = client.get(f"/api/runs/{RUN_ID}/forensics-timeline?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        kinds = [e["kind"] for e in res.json()["events"]]
        assert kinds.count("run") == 1


class TestErrors:
    def test_unknown_run_is_404(self, client: TestClient) -> None:
        res = client.get(f"/api/runs/does-not-exist/forensics-timeline?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 404

    def test_path_traversal_is_400(self, client: TestClient) -> None:
        res = client.get(f"/api/runs/..%2F..%2Fetc/forensics-timeline?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code in (400, 404)
