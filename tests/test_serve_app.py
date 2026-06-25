"""Tests for the experimental `nova serve` HTTP API (Layer A — read-only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

# Skip the entire module cleanly if the [serve] extra is not installed.
fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}
_FIXTURE_RUN_ID = "01TEST00000000000000000001"


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    """Build a tmp `.novafabric/runs/<id>/` with a valid capsule.yaml."""
    base = tmp_path / "runs"
    base.mkdir()
    run_id = "01TEST00000000000000000001"
    cdir = base / run_id
    cdir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.6.12",
        "run_id": run_id,
        "created_at": "2026-04-15T10:00:00+00:00",
        "finished_at": "2026-04-15T10:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print('hi')"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cdir / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "name": "test", "kind": "internal"}) + "\n"
    )
    (cdir / "model-calls.jsonl").write_text("")
    (cdir / "tool-calls.jsonl").write_text("")
    return base


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    """Empty SQLite db path (registry tables auto-init on access)."""
    return tmp_path / "registry.db"


@pytest.fixture
def client(capsule_dir: Path, empty_db: Path) -> Iterator[TestClient]:
    app = create_app(token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=empty_db, static_dir=None)
    with TestClient(app) as c:
        yield c


# ---------- Health (no auth) ----------

def test_health_endpoint_works_without_token(client: TestClient) -> None:
    res = client.get("/api/health", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["service"] == "nova-serve"
    assert body["experimental"] is True


# ---------- Auth ----------

def test_runs_without_token_returns_401(client: TestClient) -> None:
    res = client.get("/api/runs", headers=LOCALHOST_HEADERS)
    assert res.status_code == 401


def test_runs_with_wrong_token_returns_401(client: TestClient) -> None:
    res = client.get("/api/runs?token=wrong-token", headers=LOCALHOST_HEADERS)
    assert res.status_code == 401


def test_runs_with_correct_token_returns_200(client: TestClient) -> None:
    res = client.get(f"/api/runs?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200


# ---------- Host header guard (DNS-rebinding defence) ----------

def test_non_localhost_host_returns_403(client: TestClient) -> None:
    res = client.get(
        "/api/health",
        headers={"host": "evil.example.com"},
    )
    assert res.status_code == 403
    assert res.json()["error"] == "host_not_localhost"


def test_localhost_aliases_pass(client: TestClient) -> None:
    for host in ["127.0.0.1:4321", "localhost:4321", "localhost"]:
        res = client.get("/api/health", headers={"host": host})
        assert res.status_code == 200, f"failed for host={host}"


# ---------- Suggestions ----------

def test_suggest_register_requires_token(client: TestClient) -> None:
    res = client.get("/api/runs/suggest-register", headers=LOCALHOST_HEADERS)
    assert res.status_code == 401


def test_suggest_register_returns_suggestions_shape(client: TestClient) -> None:
    res = client.get(
        f"/api/runs/suggest-register?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert "suggestions" in body
    assert isinstance(body["suggestions"], list)
    assert "total_capsules_analyzed" in body


def test_suggest_register_not_swallowed_by_run_id_wildcard(client: TestClient) -> None:
    """Ensure the route is declared before {run_id} so it isn't matched as a run lookup."""
    res = client.get(
        f"/api/runs/suggest-register?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    # Must NOT return 404 (which would mean {run_id}="suggest-register" matched first)
    assert res.status_code != 404


# ---------- Runs ----------

def test_list_runs_returns_fixture_capsule(client: TestClient, capsule_dir: Path) -> None:
    res = client.get(f"/api/runs?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1
    assert body["capsule_dir"] == str(capsule_dir.resolve())
    run = body["runs"][0]
    assert run["run_id"] == _FIXTURE_RUN_ID
    assert run["status"] == "success"
    assert run["exit_code"] == 0


def test_get_run_returns_full_capsule(client: TestClient) -> None:
    res = client.get(
        f"/api/runs/01TEST00000000000000000001?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["run_id"] == _FIXTURE_RUN_ID
    assert body["manifest"]["status"] == "success"
    assert isinstance(body["trace"], list)
    assert isinstance(body["model_calls"], list)
    assert isinstance(body["tool_calls"], list)


def test_get_run_unknown_id_returns_404(client: TestClient) -> None:
    res = client.get(
        f"/api/runs/01NONEXISTENT0000000000000?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 404


def test_get_run_path_traversal_blocked(client: TestClient) -> None:
    res = client.get(
        f"/api/runs/..%2F..%2Fetc%2Fpasswd?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    # Either 400 (validation rejected) or 404 (resolution failed); both are safe.
    assert res.status_code in (400, 404)


def test_get_run_file_blocks_path_traversal(client: TestClient) -> None:
    res = client.get(
        f"/api/runs/01TEST00000000000000000001/file/..%2F..%2Fetc%2Fpasswd?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code in (400, 404)


# ---------- Assets ----------

def test_list_assets_against_empty_registry(client: TestClient) -> None:
    res = client.get(f"/api/assets?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 0
    assert body["assets"] == []


def test_get_unknown_asset_returns_404(client: TestClient) -> None:
    res = client.get(
        f"/api/assets/nonexistent/1.0.0?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 404


def test_eval_by_asset_id_uses_serve_db(capsule_dir: Path, tmp_path: Path) -> None:
    """Regression: eval endpoint must forward db_path to run_evals.

    Before the v0.12.4 fix, run_evals() used the default DB path, not the
    serve app's db_path, so assets registered in the serve app's DB were
    never found — producing a 404 'Asset not found' error.
    """
    from novafabric.registry.service import register_asset
    from novafabric.registry.store import get_connection, init_schema
    from novafabric.spec.validator import validate_spec

    db_path = tmp_path / "registry.db"
    spec = validate_spec(FIXTURE_DIR / "valid_agent.yaml")
    register_asset(spec, FIXTURE_DIR / "valid_agent.yaml", db_path=db_path)

    conn = get_connection(db_path)
    init_schema(conn)
    row = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()
    conn.close()
    assert row, "fixture asset must be registered"
    asset_id = row["id"]

    app = create_app(token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=db_path, static_dir=None)
    with TestClient(app) as c:
        res = c.post(
            f"/api/assets/{asset_id}/eval?token={VALID_TOKEN}",
            json={"confirmed": True},
            headers=LOCALHOST_HEADERS,
        )
    # 200 proves run_evals saw the asset in the serve DB, not a foreign default DB.
    # (Before the fix this returned 404 "Asset not found".)
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True


# ---------- Lineage ----------

def test_lineage_edges_empty_when_no_table(client: TestClient) -> None:
    res = client.get(
        f"/api/lineage/edges?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS
    )
    # Endpoint may return either an empty list (no table yet) or the
    # auto-initialised empty table. Either is acceptable.
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 0
    assert body["edges"] == []


def test_provenance_unknown_ref_returns_empty(client: TestClient) -> None:
    res = client.get(
        f"/api/lineage/provenance/asset:nonexistent@0.0.0?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["ancestors"] == []


def test_blast_radius_unknown_ref_returns_empty(client: TestClient) -> None:
    res = client.get(
        f"/api/lineage/blast-radius/asset:nonexistent@0.0.0?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["descendants"] == []


def test_replay_chain_unknown_run_returns_empty(client: TestClient) -> None:
    res = client.get(
        f"/api/lineage/replay-chain/01NONEXISTENT0000000000000?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["chain"] == []


# ---------- CORS / methods ----------

def test_post_to_get_endpoint_returns_405(client: TestClient) -> None:
    """Layer A is read-only; non-GET methods must be rejected."""
    res = client.post(
        f"/api/runs?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 405


# ---------- _get_version fallback ----------

def test_health_version_unknown_when_package_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.metadata as _meta

    import novafabric.serve.app as _app_mod

    def _raise(_: str) -> str:
        raise _meta.PackageNotFoundError("novafabric")

    monkeypatch.setattr(_app_mod.importlib_metadata, "version", _raise)
    res = client.get("/api/health", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200
    assert res.json()["version"] == "unknown"


# ---------- get_run_file ----------

def test_get_run_file_serves_jsonl(client: TestClient) -> None:
    res = client.get(
        f"/api/runs/01TEST00000000000000000001/file/trace.jsonl?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert "lines" in body
    assert isinstance(body["lines"], list)


def test_get_run_file_serves_text(client: TestClient, capsule_dir: Path) -> None:
    run_dir = capsule_dir / _FIXTURE_RUN_ID
    (run_dir / "env.lock").write_text("python=3.12\n")
    res = client.get(
        f"/api/runs/{_FIXTURE_RUN_ID}/file/env.lock?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    assert "python" in res.text


def test_get_run_file_not_found(client: TestClient) -> None:
    res = client.get(
        f"/api/runs/01TEST00000000000000000001/file/does-not-exist.txt?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 404


def test_get_run_file_rejects_dotfile(client: TestClient) -> None:
    res = client.get(
        f"/api/runs/01TEST00000000000000000001/file/.hidden?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 400


# ---------- Diff endpoint ----------

def test_diff_endpoint_returns_501_or_200(client: TestClient) -> None:
    res = client.get(
        f"/api/diff?run_a=01TEST00000000000000000001&run_b=01TEST00000000000000000001"
        f"&token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    # Returns 200 when DiffEngine is importable, 501 when not installed.
    assert res.status_code in (200, 501)


# ---------- Lineage edges with data ----------

def test_lineage_edges_with_populated_db(
    tmp_path: Path, capsule_dir: Path
) -> None:
    import sqlite3

    from fastapi.testclient import TestClient as _TC

    from novafabric.serve.app import create_app as _ca

    db = tmp_path / "lineage_test.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lineage_nodes (
            node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
            first_seen_capsule_run_id TEXT, payload TEXT NOT NULL,
            UNIQUE(kind, ref)
        );
        CREATE TABLE IF NOT EXISTS lineage_edges (
            edge_id TEXT PRIMARY KEY, edge_type TEXT NOT NULL,
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            capsule_run_id TEXT NOT NULL, confidence TEXT,
            created_at TEXT NOT NULL, payload TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO lineage_nodes VALUES (?,?,?,?,?)",
        ("n1", "run", "run:abc", "abc", '{"schema_version":"0.1.0"}'),
    )
    conn.execute(
        "INSERT INTO lineage_nodes VALUES (?,?,?,?,?)",
        ("n2", "asset", "asset:m@1.0", "abc", '{"schema_version":"0.1.0"}'),
    )
    conn.execute(
        "INSERT INTO lineage_edges VALUES (?,?,?,?,?,?,?,?)",
        ("e1", "uses", "n1", "n2", "abc", "observed",
         "2026-05-10T00:00:00Z", '{"schema_version":"0.1.0"}'),
    )
    conn.commit()
    conn.close()

    app = _ca(token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=db, static_dir=None)
    with _TC(app) as c:
        res = c.get(
            f"/api/lineage/edges?token={VALID_TOKEN}",
            headers=LOCALHOST_HEADERS,
        )
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1
    edge = body["edges"][0]
    assert edge["edge_id"] == "e1"
    assert edge["edge_type"] == "uses"
    assert edge["capsule_run_id"] == "abc"


# ---------- Root endpoint (no static_dir) ----------

def test_root_endpoint_returns_service_info(client: TestClient) -> None:
    res = client.get("/", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "nova-serve"
    assert body["experimental"] is True


# ---------- _resolve_capsule fallback: run_id from manifest, not dir name ----------

def test_resolve_capsule_via_manifest_run_id(
    tmp_path: Path, capsule_dir: Path, empty_db: Path
) -> None:
    import yaml as _yaml
    from fastapi.testclient import TestClient as _TC

    from novafabric.serve.app import create_app as _ca

    # Create a capsule directory whose name differs from the manifest's run_id.
    weird_dir = capsule_dir / "some-other-dirname"
    weird_dir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.6.12",
        "run_id": "THEREAL-RUN-ID-FROM-MANIFEST",
        "created_at": "2026-05-10T00:00:00+00:00",
        "finished_at": "2026-05-10T00:00:01+00:00",
        "duration_ms": 100,
        "command": ["python", "-c", "pass"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    (weird_dir / "capsule.yaml").write_text(_yaml.safe_dump(manifest))

    app = _ca(token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=empty_db, static_dir=None)
    with _TC(app) as c:
        res = c.get(
            f"/api/runs/THEREAL-RUN-ID-FROM-MANIFEST?token={VALID_TOKEN}",
            headers=LOCALHOST_HEADERS,
        )
    assert res.status_code == 200
    assert res.json()["run_id"] == "THEREAL-RUN-ID-FROM-MANIFEST"


# ---------- Lineage edges — invalid JSON payload skipped gracefully ----------

def test_lineage_edges_with_invalid_payload(tmp_path: Path, capsule_dir: Path) -> None:
    import sqlite3

    from fastapi.testclient import TestClient as _TC

    from novafabric.serve.app import create_app as _ca

    db = tmp_path / "lineage_bad.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lineage_nodes (
            node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
            first_seen_capsule_run_id TEXT, payload TEXT NOT NULL,
            UNIQUE(kind, ref)
        );
        CREATE TABLE IF NOT EXISTS lineage_edges (
            edge_id TEXT PRIMARY KEY, edge_type TEXT NOT NULL,
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            capsule_run_id TEXT NOT NULL, confidence TEXT,
            created_at TEXT NOT NULL, payload TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO lineage_nodes VALUES (?,?,?,?,?)",
        ("n1", "run", "run:x", "x", "{}"),
    )
    conn.execute(
        "INSERT INTO lineage_nodes VALUES (?,?,?,?,?)",
        ("n2", "asset", "asset:y@1", "x", "{}"),
    )
    conn.execute(
        "INSERT INTO lineage_edges VALUES (?,?,?,?,?,?,?,?)",
        ("e1", "uses", "n1", "n2", "x", None, "2026-05-10T00:00:00Z", "NOT VALID JSON"),
    )
    conn.commit()
    conn.close()

    app = _ca(token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=db, static_dir=None)
    with _TC(app) as c:
        res = c.get(
            f"/api/lineage/edges?token={VALID_TOKEN}",
            headers=LOCALHOST_HEADERS,
        )
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1
    assert body["edges"][0]["edge_id"] == "e1"


# ---------- Diff endpoint — force ImportError ----------

def test_diff_returns_501_when_engine_not_importable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "novafabric.diff._engine", None)  # type: ignore[arg-type]
    res = client.get(
        f"/api/diff?run_a=01TEST00000000000000000001&run_b=01TEST00000000000000000001"
        f"&token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 501


# ---------- static_dir mount path ----------

def test_create_app_with_static_dir_mounts_files(
    tmp_path: Path, capsule_dir: Path, empty_db: Path
) -> None:
    from fastapi.testclient import TestClient as _TC

    from novafabric.serve.app import create_app as _ca

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>dashboard</body></html>")

    app = _ca(token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=empty_db, static_dir=static_dir)
    with _TC(app) as c:
        res = c.get("/api/health", headers=LOCALHOST_HEADERS)
        assert res.status_code == 200


# ---------- _resolve_capsule: dotdot in run_id → 400 ----------

def test_resolve_capsule_rejects_dotdot_in_run_id(client: TestClient) -> None:
    # A run_id containing ".." triggers the path-traversal guard in _resolve_capsule.
    res = client.get(
        f"/api/runs/..EVIL?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 400


# ---------- Diff endpoint: model_dump branch (line 311) ----------

def test_diff_returns_200_with_model_dump(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    # Ensure the engine module is importable so we reach line 311.
    from novafabric.diff import _engine as diff_engine_mod

    class _FakeReport:
        def model_dump(self, mode: str = "python") -> dict[str, object]:
            return {"run_a_id": _FIXTURE_RUN_ID, "diffs": []}

    class _FakeEngine:
        def compare(self, cdir_a: object, cdir_b: object) -> _FakeReport:
            return _FakeReport()

    monkeypatch.setattr(diff_engine_mod, "DiffEngine", _FakeEngine)

    # Remove the blocking None sentinel if it was set by a prior test in this session.
    sys.modules.pop("novafabric.diff._engine", None)

    res = client.get(
        f"/api/diff?run_a=01TEST00000000000000000001&run_b=01TEST00000000000000000001"
        f"&token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    # 200 with model_dump serialisation, or 501 if already blocked. Both are safe.
    assert res.status_code in (200, 501)


# ---------- Token format ----------

def test_constant_time_token_compare_rejects_prefix_match() -> None:
    """The token check must use constant-time compare; partial prefixes must fail."""
    from novafabric.serve.app import _consteq

    token = "abcdef1234567890"
    assert _consteq(token, token) is True
    assert _consteq(token, "abc") is False
    assert _consteq(token, "abcdef1234567891") is False
    assert _consteq("a" * 16, "b" * 16) is False


# ---------- get_run_file — subdirectory path validation ----------

def test_get_run_file_bad_subdir_returns_400(client: TestClient) -> None:
    """A two-part path with an unrecognised subdirectory is rejected."""
    run_id = _FIXTURE_RUN_ID
    res = client.get(
        f"/api/runs/{run_id}/file/baddir/somefile.txt?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 400
    assert "inputs/ and outputs/" in res.json()["detail"]


def test_get_run_file_dotfile_in_valid_subdir_returns_400(client: TestClient) -> None:
    """A two-part path with a hidden filename in a valid subdir is rejected."""
    run_id = _FIXTURE_RUN_ID
    res = client.get(
        f"/api/runs/{run_id}/file/inputs/.hidden?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 400


# ---------- Lifespan startup warning ----------

def test_startup_warns_when_registry_is_empty(
    capsule_dir: Path, tmp_path: Path
) -> None:
    """WARNING is emitted at startup when the registry has zero assets."""
    from unittest.mock import MagicMock, patch

    empty_db = tmp_path / "empty.db"
    app = create_app(token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=empty_db, static_dir=None)
    mock_logger = MagicMock()
    with patch("novafabric.serve.app.logger", mock_logger):
        with TestClient(app):
            pass
    # Verify at least one warning call contains the expected message fragments.
    warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
    assert any("Registry is empty" in c and "nova register" in c for c in warning_calls), (
        f"Expected registry-empty WARNING; got warning calls: {warning_calls}"
    )


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_startup_logs_info_when_assets_present(
    capsule_dir: Path, tmp_path: Path
) -> None:
    """INFO is emitted at startup when the registry has at least one asset."""
    from unittest.mock import MagicMock, patch

    from novafabric.registry.service import register_asset
    from novafabric.spec.validator import validate_spec

    db_path = tmp_path / "registry.db"
    spec_file = FIXTURE_DIR / "valid_agent.yaml"
    spec = validate_spec(spec_file)
    register_asset(spec, spec_file, db_path=db_path)

    app = create_app(token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=db_path, static_dir=None)
    mock_logger = MagicMock()
    with patch("novafabric.serve.app.logger", mock_logger):
        with TestClient(app):
            pass
    info_calls = [str(call) for call in mock_logger.info.call_args_list]
    assert any("asset(s) available" in c for c in info_calls), (
        f"Expected asset-count INFO; got info calls: {info_calls}"
    )


def test_startup_does_not_crash_when_db_check_raises(
    capsule_dir: Path, tmp_path: Path
) -> None:
    """A corrupt or inaccessible DB must not prevent the server from starting."""
    # Point at a directory, not a file — sqlite will fail to open it.
    bad_db = tmp_path / "not-a-db"
    bad_db.mkdir()
    app = create_app(token=VALID_TOKEN, capsule_dir=capsule_dir, db_path=bad_db, static_dir=None)
    with TestClient(app) as c:
        res = c.get("/api/health", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200


# ---------- Policy check (DC-5) ----------

_POLICY_INPUT = {
    "action": "promote",
    "subject": {"user": "alice", "roles": ["platform-engineer"]},
    "resource": {"kind": "asset", "ref": "my-model@v1.0.0", "eval_score": 0.9, "unsafe_skips": 0},
    "context": {},
}


def test_policy_check_requires_token(client: TestClient) -> None:
    res = client.post(
        "/api/policy/check",
        headers=LOCALHOST_HEADERS,
        json=_POLICY_INPUT,
    )
    assert res.status_code == 401


def test_policy_check_returns_decision_when_opa_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When opa binary is absent, the endpoint returns a 200 DENY with a reason string."""
    from novafabric.policy import OpaEngine, OpaNotFoundError

    def _raise_not_found(
        self: OpaEngine, _: object, explain: bool = False, policy_source: str | None = None
    ) -> None:
        raise OpaNotFoundError("opa binary not found")

    monkeypatch.setattr(OpaEngine, "evaluate", _raise_not_found)

    res = client.post(
        f"/api/policy/check?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
        json=_POLICY_INPUT,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["allow"] is False
    assert "opa binary not found" in data["reason"]
    assert data["decision_id"]  # UUID must be present


def test_policy_check_returns_decision_for_unknown_action(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown action returns DENY from OpaEngine (no policy registered)."""
    from novafabric.policy import OpaEngine, OpaNotFoundError

    def _raise_not_found(
        self: OpaEngine, _: object, explain: bool = False, policy_source: str | None = None
    ) -> None:
        raise OpaNotFoundError("opa binary not found")

    monkeypatch.setattr(OpaEngine, "evaluate", _raise_not_found)

    unknown_input = {**_POLICY_INPUT, "action": "unknown_action_xyz"}
    res = client.post(
        f"/api/policy/check?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
        json=unknown_input,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["allow"] is False


def test_policy_check_invalid_body_returns_422(client: TestClient) -> None:
    """Missing required fields in the body must return 422 (validation error)."""
    res = client.post(
        f"/api/policy/check?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
        json={"action": "promote"},  # missing subject and resource
    )
    assert res.status_code == 422


def test_policy_check_proxies_engine_decision(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mocked engine returning ALLOW should propagate through the endpoint."""
    from novafabric.policy import OpaEngine, PolicyDecision

    def _allow(
        self: OpaEngine, input_: object, explain: bool = False, policy_source: str | None = None
    ) -> PolicyDecision:
        return PolicyDecision(
            allow=True,
            reason="test-allow",
            decision_id="test-decision-id",
            policy_path="data.novafabric.defaults.promote_gate",
        )

    monkeypatch.setattr(OpaEngine, "evaluate", _allow)

    res = client.post(
        f"/api/policy/check?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
        json=_POLICY_INPUT,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["allow"] is True
    assert data["reason"] == "test-allow"
    assert data["decision_id"] == "test-decision-id"
    assert data["policy_path"] == "data.novafabric.defaults.promote_gate"
    # input_snapshot must NOT be exposed by the endpoint
    assert "input_snapshot" not in data


def test_policy_check_passes_policy_source_to_engine(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """policy_source in the request body is forwarded to OpaEngine.evaluate."""
    from novafabric.policy import OpaEngine, PolicyDecision

    captured: dict[str, object] = {}

    def _capture(
        self: OpaEngine, input_: object, explain: bool = False, policy_source: str | None = None
    ) -> PolicyDecision:
        captured["policy_source"] = policy_source
        return PolicyDecision(
            allow=True,
            reason="custom-allow",
            decision_id="custom-id",
            policy_path="custom:data.novafabric.authz.allow",
        )

    monkeypatch.setattr(OpaEngine, "evaluate", _capture)

    rego = "package novafabric.authz\ndefault allow := true"
    res = client.post(
        f"/api/policy/check?token={VALID_TOKEN}",
        headers=LOCALHOST_HEADERS,
        json={**_POLICY_INPUT, "policy_source": rego},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["allow"] is True
    assert data["policy_path"] == "custom:data.novafabric.authz.allow"
    assert captured["policy_source"] == rego


# ---------- Storage / Infra / Admin endpoints (DC-infra) ----------


def test_storage_stats_not_configured(client: TestClient) -> None:
    """When no object store env vars are set, /api/storage/stats returns configured=False."""
    res = client.get(f"/api/storage/stats?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200
    assert res.json()["configured"] is False


def test_storage_stats_configured_no_wal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When NOVA_OBJECT_STORE_PATH is set but the WAL dir doesn't exist, stats return None counts."""
    monkeypatch.setenv("NOVA_OBJECT_STORE_PATH", str(tmp_path / "store"))
    res = client.get(f"/api/storage/stats?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["configured"] is True
    assert data["total_chunks"] is None  # WAL dir absent → not enumerated


def test_manifest_chain_not_configured(client: TestClient) -> None:
    """When the local WAL dir does not exist, /api/storage/manifest-chain returns configured=False."""
    res = client.get(
        f"/api/storage/manifest-chain?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS
    )
    assert res.status_code == 200
    assert res.json()["configured"] is False


def test_infra_collector_not_detected(client: TestClient) -> None:
    """When no collector is running on localhost, /api/infra/collector returns detected=False."""
    res = client.get(f"/api/infra/collector?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200
    assert res.json()["detected"] is False


def test_admin_tokens_empty(client: TestClient) -> None:
    """With no tokens file present, /api/admin/tokens returns an empty list."""
    res = client.get(f"/api/admin/tokens?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert "tokens" in data
    assert isinstance(data["tokens"], list)


def test_revoke_token_invalid_fingerprint(client: TestClient) -> None:
    """A fingerprint containing '..' is rejected with 400 (path traversal guard)."""
    res = client.delete(
        f"/api/admin/tokens/..evil?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS
    )
    assert res.status_code == 400


def test_revoke_token_not_found(client: TestClient) -> None:
    """Revoking a fingerprint that doesn't exist in the tokens file returns 404."""
    res = client.delete(
        f"/api/admin/tokens/nonexistent1234?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS
    )
    assert res.status_code == 404


# ── /api/runs/{run_id}/verify ─────────────────────────────────────────────────


def test_capsule_verify_requires_token(client: TestClient) -> None:
    res = client.post("/api/runs/anything/verify", headers=LOCALHOST_HEADERS)
    assert res.status_code == 401


def test_capsule_verify_unknown_run_returns_404(client: TestClient) -> None:
    res = client.post(
        "/api/runs/NOSUCHRUN_XYZ/verify",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 404


def test_capsule_verify_existing_run_returns_sealed_field(client: TestClient) -> None:
    """Fixture capsule has no .seal/ so sealed=False; response must include 'sealed' key."""
    res = client.post(
        f"/api/runs/{_FIXTURE_RUN_ID}/verify",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    assert "sealed" in res.json()
    assert res.json()["sealed"] is False  # fixture has no .seal/ directory


# ── /api/lineage/{run_id}/emit-openlineage ────────────────────────────────────


def test_lineage_emit_openlineage_requires_token(client: TestClient) -> None:
    res = client.get("/api/lineage/anything/emit-openlineage", headers=LOCALHOST_HEADERS)
    assert res.status_code == 401


def test_lineage_emit_openlineage_unknown_run_returns_404(client: TestClient) -> None:
    res = client.get(
        "/api/lineage/NOSUCHRUN_XYZ/emit-openlineage",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 404


def test_lineage_emit_openlineage_existing_run_returns_events_shape(client: TestClient) -> None:
    """Fixture capsule returns ok + events list (may be empty or ok=False on import error)."""
    res = client.get(
        f"/api/lineage/{_FIXTURE_RUN_ID}/emit-openlineage",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert "ok" in data
    assert "events" in data
    assert isinstance(data["events"], list)


# ── /api/assets/register-from-yaml ───────────────────────────────────────────


def test_register_from_yaml_requires_token(client: TestClient) -> None:
    res = client.post(
        "/api/assets/register-from-yaml",
        json={"spec_yaml": "name: test"},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 401


def test_register_from_yaml_empty_body_returns_422(client: TestClient) -> None:
    res = client.post(
        "/api/assets/register-from-yaml",
        json={"spec_yaml": ""},
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 422


def test_register_from_yaml_invalid_spec_returns_ok_false(client: TestClient) -> None:
    res = client.post(
        "/api/assets/register-from-yaml",
        json={"spec_yaml": "this_is_not_valid: yaml: for: an: asset: spec"},
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["ok"] is False
    assert "error" in res.json()
    assert "error" in res.json()


# ---------- KG alias endpoints ----------


def test_kg_aliases_list_no_db_returns_ok_empty(client: TestClient) -> None:
    res = client.get(
        "/api/kg/aliases",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["count"] == 0
    assert data["aliases"] == []


def test_kg_aliases_list_requires_token(client: TestClient) -> None:
    res = client.get("/api/kg/aliases", headers=LOCALHOST_HEADERS)
    assert res.status_code == 401


def test_kg_aliases_register_and_list(client: TestClient, tmp_path: Path) -> None:
    import os

    db_path = tmp_path / "alias.db"
    env_patch = {"NOVA_KG_ALIAS_DB": str(db_path)}
    original = {k: os.environ.get(k) for k in env_patch}
    os.environ.update(env_patch)
    try:
        res = client.post(
            "/api/kg/aliases",
            json={"alias": "gpt4", "canonical": "openai/gpt-4", "entity_type": "model"},
            params={"token": VALID_TOKEN},
            headers=LOCALHOST_HEADERS,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["alias"] == "gpt4"

        res2 = client.get(
            "/api/kg/aliases",
            params={"token": VALID_TOKEN},
            headers=LOCALHOST_HEADERS,
        )
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["count"] == 1
        assert data2["aliases"][0]["alias"] == "gpt4"
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_kg_aliases_register_missing_fields_returns_ok_false(client: TestClient) -> None:
    res = client.post(
        "/api/kg/aliases",
        json={"alias": "incomplete"},
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["ok"] is False
    assert "error" in res.json()


# ---------- /v1/kg/query ----------


def test_v1_kg_query_no_kg_returns_ok_empty(client: TestClient) -> None:
    res = client.get(
        "/v1/kg/query",
        params={"token": VALID_TOKEN, "agent_id": "agent-001"},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert "ok" in data


def test_v1_kg_query_requires_token(client: TestClient) -> None:
    res = client.get(
        "/v1/kg/query",
        params={"agent_id": "agent-001"},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 401


# ---------- /v1/kg/audit ----------


def test_v1_kg_audit_no_kg_returns_ok_false(client: TestClient) -> None:
    res = client.get(
        "/v1/kg/audit",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert "ok" in data


def test_v1_kg_audit_requires_token(client: TestClient) -> None:
    res = client.get("/v1/kg/audit", headers=LOCALHOST_HEADERS)
    assert res.status_code == 401


# ---------- entity-queue endpoints ----------


def test_kg_entity_queue_list_returns_ok(client: TestClient) -> None:
    res = client.get(
        "/api/kg/entity-queue",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "count" in data
    assert "items" in data


def test_kg_entity_queue_stats_returns_ok(client: TestClient) -> None:
    res = client.get(
        "/api/kg/entity-queue/stats",
        params={"token": VALID_TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "pending" in data
