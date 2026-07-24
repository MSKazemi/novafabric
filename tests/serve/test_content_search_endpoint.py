"""/api/runs/search scope=content|all tests (ADR-0204 P1, experimental).

The critical pin: with `scope` omitted (or `scope=meta`) the endpoint's
behavior and response shape are byte-identical to the pre-ADR-0204
run_id/command matching. Content scope returns snippet matches; environments
without FTS5 degrade to a 501 error envelope for content scopes only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from novafabric.query.content_index import fts5_available, maybe_index_capsule
from novafabric.registry.runs_cache import ensure_runs_cache, upsert_run
from novafabric.registry.store import get_connection, init_schema
from novafabric.serve.app import create_app

TOKEN = "testtoken"
H = {"host": "127.0.0.1:4321"}

requires_fts5 = pytest.mark.skipif(
    not fts5_available(), reason="sqlite build lacks FTS5"
)


def _make_capsule(base: Path, run_id: str, message: str) -> Path:
    cap = base / run_id
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text(yaml.dump({
        "run_id": run_id,
        "status": "success",
        "created_at": "2026-07-24T10:00:00Z",
        "command": ["python", "agent.py"],
    }))
    (cap / "model-calls.jsonl").write_text(json.dumps({
        "gen_ai.request.messages": [{"role": "user", "content": message}],
    }) + "\n")
    return cap


@pytest.fixture()
def harness(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    base = tmp_path / "capsules"
    base.mkdir()
    db = tmp_path / "registry.db"
    conn = get_connection(db)
    init_schema(conn)
    ensure_runs_cache(conn)
    # Two runs in the metadata cache; one has an ingested content capsule.
    upsert_run(conn, {
        "run_id": "run-content", "status": "success",
        "created_at": "2026-07-24T10:00:00Z", "command": ["python", "agent.py"],
    })
    upsert_run(conn, {
        "run_id": "run-meta-only", "status": "success",
        "created_at": "2026-07-24T11:00:00Z",
        "command": ["nova", "invoice-tool"],
    })
    cap = _make_capsule(base, "run-content", "please pay invoice INV-2291 now")
    if fts5_available():
        maybe_index_capsule(conn, cap, "run-content")
    conn.commit()
    conn.close()
    app = create_app(token=TOKEN, capsule_dir=base, db_path=db, static_dir=None)
    return TestClient(app), base, db


def _get(client: TestClient, **params: str | int) -> object:
    return client.get(
        "/api/runs/search", params={"token": TOKEN, **params}, headers=H
    )


# ── regression pin: scope omitted == pre-change behavior ─────────────────


def test_scope_omitted_keeps_old_metadata_matching(
    harness: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = harness
    r = _get(client, q="invoice-tool")
    assert r.status_code == 200
    data = r.json()
    # Exact pre-ADR-0204 response shape: no snippet_markers, no matches key.
    assert set(data.keys()) == {"items", "next_cursor", "total_approx"}
    assert [i["run_id"] for i in data["items"]] == ["run-meta-only"]
    assert all("matches" not in i for i in data["items"])
    # Old behavior: q matches run_id too …
    r = _get(client, q="run-content")
    assert [i["run_id"] for i in r.json()["items"]] == ["run-content"]
    # … but never capsule content.
    r = _get(client, q="INV-2291")
    assert r.json()["items"] == []


def test_scope_meta_identical_to_omitted(
    harness: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = harness
    for q in ("invoice-tool", "run-content", "INV-2291", ""):
        params = {"q": q} if q else {}
        assert (
            _get(client, **params).json()
            == _get(client, scope="meta", **params).json()
        )


def test_invalid_scope_rejected(
    harness: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = harness
    assert _get(client, scope="everything", q="x").status_code == 422


# ── content scope ─────────────────────────────────────────────────────────


@requires_fts5
def test_scope_content_returns_snippets(
    harness: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = harness
    r = _get(client, scope="content", q="INV-2291")
    assert r.status_code == 200
    data = r.json()
    assert data["snippet_markers"] == ["«", "»"]
    assert data["next_cursor"] is None
    (item,) = data["items"]
    assert item["run_id"] == "run-content"
    assert item["created_at"] == "2026-07-24T10:00:00Z"
    (match,) = item["matches"]
    assert match["stream"] == "model-call-messages"
    assert match["ref"] == "model-calls.jsonl"
    assert match["line_no"] == 1
    assert "«INV»" in match["snippet"] or "«INV-2291»" in match["snippet"]


@requires_fts5
def test_scope_content_requires_q(
    harness: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = harness
    r = _get(client, scope="content")
    assert r.status_code == 400
    r = _get(client, scope="content", q="   ")
    assert r.status_code == 400


@requires_fts5
def test_scope_content_operator_injection_is_literal(
    harness: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = harness
    for q in ('pay OR nothing', 'x" OR "y', "NEAR(", "inv*"):
        r = _get(client, scope="content", q=q)
        assert r.status_code == 200, q
    # AND semantics with a literal OR token: no doc contains 'OR' → no hits.
    assert _get(client, scope="content", q="pay OR nothing").json()["items"] == []
    assert [
        i["run_id"]
        for i in _get(client, scope="content", q="inv*").json()["items"]
    ] == ["run-content"]


@requires_fts5
def test_scope_all_merges_content_first_then_metadata(
    harness: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = harness
    # 'invoice' hits run-content in content AND run-meta-only in metadata
    # (command_json contains 'invoice-tool').
    r = _get(client, scope="all", q="invoice")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["run_id"] for i in items] == ["run-content", "run-meta-only"]
    assert "matches" in items[0]
    assert "matches" not in items[1]


@requires_fts5
def test_scope_all_dedups_by_run_id(
    harness: tuple[TestClient, Path, Path],
) -> None:
    client, _, _ = harness
    # 'agent.py' matches run-content in BOTH scopes (capsule-yaml command
    # doc and command_json metadata) — it must appear once, content-ranked.
    r = _get(client, scope="all", q="agent.py")
    items = r.json()["items"]
    ids = [i["run_id"] for i in items]
    assert ids.count("run-content") == 1


# ── FTS5-unavailable degradation ──────────────────────────────────────────


def test_fts5_unavailable_returns_501_envelope_and_meta_still_works(
    harness: tuple[TestClient, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.query import content_index as ci

    monkeypatch.setattr(ci, "fts5_available", lambda: False)
    client, _, _ = harness
    r = _get(client, scope="content", q="INV-2291")
    assert r.status_code == 501
    body = r.json()
    assert body["error"]["code"] == "fts5_unavailable"
    assert "FTS5" in body["error"]["message"]
    # Metadata search is unaffected.
    r = _get(client, q="invoice-tool")
    assert r.status_code == 200
    assert [i["run_id"] for i in r.json()["items"]] == ["run-meta-only"]
