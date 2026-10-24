"""``POST /api/kg/ingest`` takes NovaSeal verification from the capsule, not the caller
(review serve-09).

``verified: true`` in the request body made every edge from the capsule count as
NovaSeal-verified (``confidence`` 1.0) whatever the capsule carried: any token
holder could raise the graph's confidence for an unsealed capsule.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("kuzu")

from novafabric.capture.orchestrator import CaptureOrchestrator  # noqa: E402
from novafabric.kg.store import KGStore  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

TOKEN = "kg-verify-token"
HEADERS = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path, Path]:
    kg = tmp_path / "kg" / "nova_kg.kuzu"
    monkeypatch.setenv("NOVA_KG_PATH", str(kg))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    store = tmp_path / "runs"
    store.mkdir()
    client = TestClient(create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "r.db"))
    return client, store, kg


def _unsealed_capsule_with_a_model_call(store: Path) -> Path:
    cap = CaptureOrchestrator(base_dir=store).run(command=[sys.executable, "-c", "pass"]).capsule_dir
    assert not (cap / ".seal").exists()
    (cap / "model-calls.jsonl").write_text(json.dumps({
        "model_call_id": "m1", "parent_span_id": "agent-span",
        "gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o",
    }) + "\n")
    return cap


def test_a_caller_cannot_mark_an_unsealed_capsule_verified(
    stack: tuple[TestClient, Path, Path]
) -> None:
    client, store, kg = stack
    cap = _unsealed_capsule_with_a_model_call(store)
    resp = client.post("/api/kg/ingest", headers=HEADERS,
                       json={"capsule_path": cap.name, "verified": True})
    body = resp.json()
    assert body["ok"] is True, body
    assert "verified" in body["note"]  # says the flag was not honoured
    (edge,) = KGStore(kg).query_agent_models("agent-span")
    assert edge["confidence"] == 0.0
