"""``GET /api/runs/{run_id}/replayability`` -- the Replay workspace read surface (issue #269).

The dashboard's Replay workspace explains, per mode, what a replay would execute. It
must get the servability answer from the same classifier ``nova replay`` uses, must
never execute or write anything to answer, and must never offer to start a mode that
re-runs the workload. These tests pin all three against a real ``create_app``.

See src/novafabric/serve/routers/replay_workspace.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.replay._replayability_summary import (  # noqa: E402
    REPLAYABILITY_REF,
    assess,
    render,
)
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.introspect import iter_routes  # noqa: E402
from novafabric.serve.routers.replay_workspace import (  # noqa: E402
    DASHBOARD_STARTABLE_MODES,
    MODE_ROUTE,
)

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"
RUN_ID = "01KREPLAYWORKSPACE00000001"
ADAPTER_RUN_ID = "01KREPLAYWORKSPACE00000002"


def _manifest(run_id: str = RUN_ID, **overrides: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": "2026-11-27T10:00:00+00:00",
        "finished_at": "2026-11-27T10:00:05+00:00",
        "command": ["python", "agent.py"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
    }
    base.update(overrides)
    return base


def _openai() -> dict[str, Any]:
    return {
        "model_call_id": "m-1",
        "gen_ai.system": "openai",
        "status": "success",
        "gen_ai.response.choices": [{"index": 0, "message": {"content": "hi"}}],
    }


def _mistral() -> dict[str, Any]:
    # A registry model host with no recorded response: not servable (ADR-0319).
    return {
        "model_call_id": "mistral-1",
        "gen_ai.system": "mistral",
        "endpoint": "https://api.mistral.ai/v1/chat/completions",
        "status": "success",
        "gen_ai.response.choices": [],
    }


def _write_capsule(base: Path, run_id: str, manifest: dict[str, Any], models: list[Any]) -> Path:
    cdir = base / run_id
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cdir / "model-calls.jsonl").write_text("".join(json.dumps(m) + "\n" for m in models))
    (cdir / "tool-calls.jsonl").write_text("")
    return cdir


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    _write_capsule(base, RUN_ID, _manifest(), [_openai(), _mistral()])
    _write_capsule(
        base,
        ADAPTER_RUN_ID,
        _manifest(ADAPTER_RUN_ID, command=["@langgraph:demo"], capture_mode="sdk-decorator"),
        [_openai()],
    )
    return base


@pytest.fixture
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


def _get(client: TestClient, run_id: str = RUN_ID) -> Any:
    return client.get(f"/api/runs/{run_id}/replayability?{TOKEN_Q}", headers=HEADERS)


def _tree(root: Path) -> list[tuple[str, int]]:
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*"))


class TestAuthAndLookup:
    def test_requires_token(self, client: TestClient) -> None:
        res = client.get(f"/api/runs/{RUN_ID}/replayability", headers=HEADERS)
        assert res.status_code == 401

    def test_unknown_run_is_404(self, client: TestClient) -> None:
        assert _get(client, "01KNOSUCHRUN00000000000000").status_code == 404


class TestCurrentClassification:
    def test_unservable_model_call_predicts_a_strict_refusal(self, client: TestClient) -> None:
        res = _get(client)
        assert res.status_code == 200, res.text
        body = res.json()
        current = body["current"]
        assert body["current_error"] is None
        assert current["schema"] == "novafabric.replayability/1"
        assert current["strict_mocked"] == "refuse"
        assert current["replayable"] == "partial"
        assert current["refusal"]["type"] == "ModelSurfaceNotServable"
        assert current["model_calls"]["recorded"] == 2
        assert current["model_calls"]["servable"] == 1
        assert current["model_calls"]["not_servable"] == 1
        (reason,) = current["reasons"]
        assert reason["code"] == "model_surface_not_servable"
        assert reason["refuses_strict"] is True

    def test_agrees_with_the_shared_classifier(self, client: TestClient) -> None:
        """Same predicates as ``nova replay`` -- not a second rule set."""
        expected = assess(_manifest(), [_openai(), _mistral()], []).as_dict()
        current = _get(client).json()["current"]
        for key in ("replayable", "strict_mocked", "model_calls", "tool_calls", "reexecution"):
            assert current[key] == expected[key], key

    def test_capsule_with_no_command_is_not_replayable(self, client: TestClient) -> None:
        current = _get(client, ADAPTER_RUN_ID).json()["current"]
        assert current["replayable"] == "none"
        assert current["refusal"]["type"] == "CapsuleNotReplayable"
        assert current["reexecution"]["reexecutable"] is False

    def test_a_classifier_error_is_reported_not_a_500(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        cdir = capsule_dir / RUN_ID
        (cdir / "model-calls.jsonl").unlink()
        (cdir / "model-calls.jsonl").mkdir()  # present but unusable: replay refuses it
        res = _get(client)
        assert res.status_code == 200
        body = res.json()
        assert body["current"] is None
        assert body["current_error"]


class TestRecordedSummary:
    def test_absent_for_a_capsule_written_without_one(self, client: TestClient) -> None:
        body = _get(client).json()
        assert body["recorded_state"] == "absent"
        assert body["recorded"] is None

    def test_present_is_returned_as_written(self, client: TestClient, capsule_dir: Path) -> None:
        doc = assess(_manifest(), [_openai()], [])
        (capsule_dir / RUN_ID / REPLAYABILITY_REF).write_text(render(doc))
        body = _get(client).json()
        assert body["recorded_state"] == "present"
        assert body["recorded"] == json.loads(render(doc))
        # The recorded prediction is kept apart from the current one: they differ here.
        assert body["recorded"]["replayable"] == "full"
        assert body["current"]["replayable"] == "partial"

    def test_malformed_is_invalid_not_a_500(self, client: TestClient, capsule_dir: Path) -> None:
        (capsule_dir / RUN_ID / REPLAYABILITY_REF).write_text("{not json")
        res = _get(client)
        assert res.status_code == 200
        body = res.json()
        assert body["recorded_state"] == "invalid"
        assert body["recorded"] is None
        assert body["recorded_error"]
        assert body["current"] is not None


class TestNoSideEffects:
    def test_reading_writes_nothing(self, client: TestClient, capsule_dir: Path) -> None:
        before = _tree(capsule_dir)
        assert _get(client).status_code == 200
        assert _get(client, ADAPTER_RUN_ID).status_code == 200
        assert _tree(capsule_dir) == before


class TestDashboardModes:
    def test_modes_that_run_the_workload_are_never_startable(self, client: TestClient) -> None:
        modes = _get(client).json()["dashboard_modes"]
        assert modes["mocked"] is False
        assert modes["intervention"] is False
        assert modes == dict(DASHBOARD_STARTABLE_MODES)

    def test_startable_modes_match_the_mounted_replay_routes(self, client: TestClient) -> None:
        prefix = "/api/runs/{run_id}/replay/"
        mounted = {
            info.path[len(prefix) :]
            for info in iter_routes(client.app)
            if "POST" in info.methods and info.path.startswith(prefix)
        }
        startable = {MODE_ROUTE[m] for m, ok in DASHBOARD_STARTABLE_MODES.items() if ok}
        # Every mode the workspace offers to start has a route, and every replay
        # route is accounted for: a new one (e.g. a mocked endpoint) must be
        # classified here before the dashboard can treat it as startable.
        assert mounted == startable
        assert "mocked" not in mounted and "intervention" not in mounted
