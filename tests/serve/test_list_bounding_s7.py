"""S7 scale slice — keyset fix + bounded-pagination retrofit (ADR-0199, B2).

Pins the bounds retrofitted onto the remaining unbounded list surfaces:

* ``GET /api/runs/search`` — the fast path now passes the decoded keyset
  cursor straight into ``query_runs`` (no cursor→OFFSET COUNT(*) conversion).
* ``GET /api/policy/recent-decisions`` / ``GET /api/policy/explain`` — O(page)
  reverse tail reads with a byte-offset cursor (same contract as /api/audit);
  explain is now newest-first.
* ``GET /api/lineage/edges`` — keyset cursor over (created_at, edge_id),
  true ``total``, ``truncated``.
* ``GET /api/kg/entity-queue`` — SQL-pushed-down ``limit`` + true ``total``.
* ``GET /api/admin/tokens`` — byte-offset cursor over tokens.jsonl.
* ``GET /api/admin/api-keys`` — SQL-pushed-down ``limit`` + COUNT total.
* ``GET /api/assets/{asset_id}`` — bounded eval history.

House rules pinned per endpoint: page ≤ limit, true total, ``truncated``
flips correctly, cursor pages are disjoint + complete, over-cap limit → 422,
and a malformed *keyset* cursor decodes to None ⇒ first page (byte-offset
cursors are typed ``int`` and malformed values 422 at validation, matching
/api/audit). Plus unit tests for the pushed-down store reads.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.registry.runs_cache import ensure_runs_cache, upsert_run  # noqa: E402
from novafabric.registry.store import get_connection, init_schema  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-s7-bounding"
HEADERS = {"host": "127.0.0.1:4321"}
AUTH = {"token": VALID_TOKEN}


def _make_client(tmp_path: Path, db: Path | None = None) -> TestClient:
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=runs,
        db_path=db if db is not None else tmp_path / "registry.db",
        static_dir=None,
    )
    return TestClient(app)


# ── /api/runs/search — true keyset fast path ─────────────────────────────────


class TestRunsSearchKeyset:
    @pytest.fixture()
    def client(self, tmp_path: Path) -> TestClient:
        db = tmp_path / "registry.db"
        conn = get_connection(db)
        init_schema(conn)
        ensure_runs_cache(conn)
        # 7 runs, two sharing one timestamp to exercise the (ts, id) tie-break.
        stamps = [
            ("r7", "2026-07-30T10:00:07Z"),
            ("r6", "2026-07-30T10:00:06Z"),
            ("r5", "2026-07-30T10:00:05Z"),
            ("r4b", "2026-07-30T10:00:04Z"),
            ("r4a", "2026-07-30T10:00:04Z"),
            ("r2", "2026-07-30T10:00:02Z"),
            ("r1", "2026-07-30T10:00:01Z"),
        ]
        for run_id, ts in stamps:
            upsert_run(
                conn,
                {"run_id": run_id, "status": "success", "created_at": ts, "command": []},
            )
        conn.commit()
        conn.close()
        return _make_client(tmp_path, db)

    def _get(self, client: TestClient, **params: str | int) -> dict:
        r = client.get(
            "/api/runs/search", params={**AUTH, **params}, headers=HEADERS
        )
        assert r.status_code == 200
        return r.json()

    def test_deep_cursor_walk_no_overlap_no_gap(self, client: TestClient) -> None:
        full = [i["run_id"] for i in self._get(client, limit=50)["items"]]
        assert len(full) == 7

        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        for _ in range(10):
            data = self._get(
                client, limit=2, **({"cursor": cursor} if cursor else {})
            )
            assert len(data["items"]) <= 2
            seen.extend(i["run_id"] for i in data["items"])
            pages += 1
            cursor = data.get("next_cursor")
            if not cursor:
                break
        assert pages >= 3
        assert seen == full  # disjoint + complete, order preserved

    def test_total_reported(self, client: TestClient) -> None:
        assert self._get(client, limit=2)["total_approx"] == 7

    def test_malformed_cursor_is_first_page(self, client: TestClient) -> None:
        first = self._get(client, limit=3)["items"]
        garbled = self._get(client, limit=3, cursor="!!not-a-cursor!!")["items"]
        assert garbled == first

    def test_limit_over_cap_rejected(self, client: TestClient) -> None:
        r = client.get(
            "/api/runs/search", params={**AUTH, "limit": 9999}, headers=HEADERS
        )
        assert r.status_code == 422


# ── /api/policy/recent-decisions + /api/policy/explain ──────────────────────


def _write_audit(path: Path, n: int, decision_id: str | None = None) -> list[str]:
    """Append n audit entries; returns their decision ids oldest-first."""
    ids: list[str] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for i in range(n):
            did = decision_id or f"dec-{i:04d}"
            f.write(
                json.dumps(
                    {
                        "audit_id": f"aud-{i:04d}",
                        "ts": f"2026-07-30T09:{i // 60:02d}:{i % 60:02d}Z",
                        "action": "policy_eval",
                        "args": {"decision_id": did},
                        "result": "ok",
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            ids.append(did)
    return ids


class TestPolicyRecentDecisions:
    @pytest.fixture()
    def stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[TestClient, Path]:
        audit_file = tmp_path / "dashboard-audit.jsonl"
        monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(audit_file))
        return _make_client(tmp_path), audit_file

    def test_bounded_and_truncated(self, stack: tuple[TestClient, Path]) -> None:
        client, audit_file = stack
        _write_audit(audit_file, 5)
        r = client.get(
            "/api/policy/recent-decisions",
            params={**AUTH, "limit": 2},
            headers=HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["decision_ids"]) == 2
        # newest-first
        assert body["decision_ids"] == ["dec-0004", "dec-0003"]
        assert body["truncated"] is True
        assert body["next_cursor"] is not None

    def test_cursor_pages_disjoint_and_complete(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, audit_file = stack
        all_ids = _write_audit(audit_file, 5)
        seen: list[str] = []
        cursor: int | None = None
        for _ in range(10):
            params: dict = {**AUTH, "limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            body = client.get(
                "/api/policy/recent-decisions", params=params, headers=HEADERS
            ).json()
            seen.extend(body["decision_ids"])
            cursor = body["next_cursor"]
            if cursor is None:
                assert body["truncated"] is False
                break
        assert seen == list(reversed(all_ids))

    def test_not_truncated_within_limit(self, stack: tuple[TestClient, Path]) -> None:
        client, audit_file = stack
        _write_audit(audit_file, 3)
        body = client.get(
            "/api/policy/recent-decisions",
            params={**AUTH, "limit": 50},
            headers=HEADERS,
        ).json()
        assert len(body["decision_ids"]) == 3
        assert body["truncated"] is False
        assert body["next_cursor"] is None

    def test_limit_over_cap_rejected(self, stack: tuple[TestClient, Path]) -> None:
        client, _ = stack
        r = client.get(
            "/api/policy/recent-decisions",
            params={**AUTH, "limit": 999},
            headers=HEADERS,
        )
        assert r.status_code == 422

    def test_malformed_byte_cursor_rejected(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        # Byte-offset cursors are ints (same as /api/audit); garbage → 422.
        client, _ = stack
        r = client.get(
            "/api/policy/recent-decisions",
            params={**AUTH, "cursor": "garbage"},
            headers=HEADERS,
        )
        assert r.status_code == 422

    def test_missing_log_keeps_shape(self, stack: tuple[TestClient, Path]) -> None:
        client, _ = stack
        body = client.get(
            "/api/policy/recent-decisions", params=AUTH, headers=HEADERS
        ).json()
        assert body == {"decision_ids": [], "next_cursor": None, "truncated": False}


class TestPolicyExplain:
    @pytest.fixture()
    def stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[TestClient, Path]:
        audit_file = tmp_path / "dashboard-audit.jsonl"
        monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(audit_file))
        return _make_client(tmp_path), audit_file

    def test_bounded_newest_first_with_cursor_walk(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, audit_file = stack
        _write_audit(audit_file, 5, decision_id="dec-same")
        _write_audit(audit_file, 2)  # non-matching noise, scanned but filtered

        seen: list[str] = []
        cursor: int | None = None
        for _ in range(10):
            params: dict = {**AUTH, "decision_id": "dec-same", "limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            body = client.get(
                "/api/policy/explain", params=params, headers=HEADERS
            ).json()
            assert body["ok"] is True
            assert body["decision_id"] == "dec-same"
            assert body["count"] == len(body["entries"])
            assert len(body["entries"]) <= 2
            seen.extend(e["audit_id"] for e in body["entries"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        # All 5 matches found, newest-first, disjoint + complete.
        assert seen == [f"aud-{i:04d}" for i in reversed(range(5))]

    def test_truncated_flips(self, stack: tuple[TestClient, Path]) -> None:
        client, audit_file = stack
        _write_audit(audit_file, 3, decision_id="dec-x")
        body = client.get(
            "/api/policy/explain",
            params={**AUTH, "decision_id": "dec-x", "limit": 2},
            headers=HEADERS,
        ).json()
        assert body["truncated"] is True
        body = client.get(
            "/api/policy/explain",
            params={**AUTH, "decision_id": "dec-x", "limit": 100},
            headers=HEADERS,
        ).json()
        assert body["truncated"] is False
        assert body["next_cursor"] is None

    def test_limit_over_cap_rejected(self, stack: tuple[TestClient, Path]) -> None:
        client, _ = stack
        r = client.get(
            "/api/policy/explain",
            params={**AUTH, "decision_id": "x", "limit": 1001},
            headers=HEADERS,
        )
        assert r.status_code == 422

    def test_missing_log_keeps_legacy_keys(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        body = client.get(
            "/api/policy/explain",
            params={**AUTH, "decision_id": "abc"},
            headers=HEADERS,
        ).json()
        assert body["ok"] is False
        assert body["entries"] == []
        assert body["count"] == 0
        assert body["truncated"] is False


# ── /api/lineage/edges — keyset cursor + total + truncated ───────────────────


class TestLineageEdgesBounding:
    @pytest.fixture()
    def client(self, tmp_path: Path) -> TestClient:
        db = tmp_path / "registry.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
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
            """
        )
        conn.execute(
            "INSERT INTO lineage_nodes VALUES (?,?,?,?,?)",
            ("n1", "run", "run:abc", "abc", '{"schema_version":"0.1.0"}'),
        )
        conn.execute(
            "INSERT INTO lineage_nodes VALUES (?,?,?,?,?)",
            ("n2", "asset", "asset:m@1.0", "abc", '{"schema_version":"0.1.0"}'),
        )
        # 7 edges; two share a created_at to exercise the edge_id tie-break.
        stamps = [
            ("e7", "2026-07-30T00:00:07Z"),
            ("e6", "2026-07-30T00:00:06Z"),
            ("e5", "2026-07-30T00:00:05Z"),
            ("e4b", "2026-07-30T00:00:04Z"),
            ("e4a", "2026-07-30T00:00:04Z"),
            ("e2", "2026-07-30T00:00:02Z"),
            ("e1", "2026-07-30T00:00:01Z"),
        ]
        for edge_id, ts in stamps:
            conn.execute(
                "INSERT INTO lineage_edges VALUES (?,?,?,?,?,?,?,?)",
                (edge_id, "uses", "n1", "n2", "abc", "observed", ts,
                 '{"schema_version":"0.1.0"}'),
            )
        conn.commit()
        conn.close()
        return _make_client(tmp_path, db)

    def _get(self, client: TestClient, **params: str | int) -> dict:
        r = client.get(
            "/api/lineage/edges", params={**AUTH, **params}, headers=HEADERS
        )
        assert r.status_code == 200
        return r.json()

    def test_bounded_with_total_and_truncated(self, client: TestClient) -> None:
        body = self._get(client, limit=3)
        assert body["count"] == 3
        assert len(body["edges"]) == 3
        assert body["total"] == 7
        assert body["truncated"] is True
        assert body["next_cursor"]

    def test_not_truncated_within_limit(self, client: TestClient) -> None:
        body = self._get(client, limit=100)
        assert body["count"] == 7
        assert body["total"] == 7
        assert body["truncated"] is False
        assert body["next_cursor"] is None

    def test_cursor_pages_disjoint_and_complete(self, client: TestClient) -> None:
        full = [e["edge_id"] for e in self._get(client, limit=100)["edges"]]
        assert full == ["e7", "e6", "e5", "e4b", "e4a", "e2", "e1"]
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            body = self._get(
                client, limit=2, **({"cursor": cursor} if cursor else {})
            )
            seen.extend(e["edge_id"] for e in body["edges"])
            cursor = body.get("next_cursor")
            if not cursor:
                break
        assert seen == full

    def test_malformed_cursor_is_first_page(self, client: TestClient) -> None:
        first = self._get(client, limit=3)["edges"]
        garbled = self._get(client, limit=3, cursor="%%bogus%%")["edges"]
        assert garbled == first

    def test_limit_over_cap_rejected(self, client: TestClient) -> None:
        r = client.get(
            "/api/lineage/edges", params={**AUTH, "limit": 999999}, headers=HEADERS
        )
        assert r.status_code == 422

    def test_empty_table_shape(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        body = self._get(client)
        assert body["count"] == 0
        assert body["edges"] == []
        assert body["total"] == 0
        assert body["truncated"] is False


# ── /api/kg/entity-queue — SQL-pushed-down limit ─────────────────────────────


class TestEntityQueueBounding:
    @pytest.fixture()
    def stack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[TestClient, Path]:
        queue_db = tmp_path / "kg" / "review_queue.db"
        monkeypatch.setenv("NOVA_KG_QUEUE_DB", str(queue_db))
        return _make_client(tmp_path), queue_db

    @staticmethod
    def _enqueue(queue_db: Path, n: int) -> None:
        from novafabric.kg.review_queue import HumanReviewQueueWriter, ReviewItem

        q = HumanReviewQueueWriter(db_path=queue_db)
        for i in range(n):
            q.enqueue(
                ReviewItem(alias=f"model-{i:03d}", entity_type="model", confidence=0.3)
            )
        q.close()

    def test_bounded_with_total_and_truncated(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, queue_db = stack
        self._enqueue(queue_db, 5)
        r = client.get(
            "/api/kg/entity-queue", params={**AUTH, "limit": 2}, headers=HEADERS
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["count"] == 2
        assert len(body["items"]) == 2
        assert body["total"] == 5
        assert body["truncated"] is True

    def test_not_truncated_within_limit(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, queue_db = stack
        self._enqueue(queue_db, 3)
        body = client.get(
            "/api/kg/entity-queue", params=AUTH, headers=HEADERS
        ).json()
        assert body["count"] == 3
        assert body["total"] == 3
        assert body["truncated"] is False

    def test_limit_over_cap_rejected(self, stack: tuple[TestClient, Path]) -> None:
        client, _ = stack
        r = client.get(
            "/api/kg/entity-queue", params={**AUTH, "limit": 1001}, headers=HEADERS
        )
        assert r.status_code == 422


# ── /api/admin/tokens — byte-offset cursor over tokens.jsonl ─────────────────


class TestAdminTokensBounding:
    @pytest.fixture()
    def client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> TestClient:
        monkeypatch.setattr(
            Path, "home", classmethod(lambda cls: tmp_path)  # type: ignore[attr-defined]
        )
        tokens_file = tmp_path / ".novafabric" / "tokens.jsonl"
        tokens_file.parent.mkdir(parents=True, exist_ok=True)
        with tokens_file.open("a", encoding="utf-8") as f:
            for i in range(5):
                f.write(
                    json.dumps(
                        {
                            "label": f"t{i}",
                            "token": f"secret-{i}",
                            "fingerprint": f"fp{i:04d}",
                            "created_at": f"2026-07-30T00:00:0{i}Z",
                            "revoked": False,
                        }
                    )
                    + "\n"
                )
        return _make_client(tmp_path)

    def test_bounded_newest_first_with_truncated(self, client: TestClient) -> None:
        r = client.get(
            "/api/admin/tokens", params={**AUTH, "limit": 2}, headers=HEADERS
        )
        assert r.status_code == 200
        body = r.json()
        assert [t["fingerprint"] for t in body["tokens"]] == ["fp0004", "fp0003"]
        assert body["session_token_fingerprint"] == VALID_TOKEN[:8]
        assert body["truncated"] is True
        assert body["next_cursor"] is not None
        # raw token values must never appear
        assert "secret-" not in r.text

    def test_cursor_pages_disjoint_and_complete(self, client: TestClient) -> None:
        seen: list[str] = []
        cursor: int | None = None
        for _ in range(10):
            params: dict = {**AUTH, "limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            body = client.get(
                "/api/admin/tokens", params=params, headers=HEADERS
            ).json()
            seen.extend(t["fingerprint"] for t in body["tokens"])
            cursor = body["next_cursor"]
            if cursor is None:
                assert body["truncated"] is False
                break
        assert seen == [f"fp{i:04d}" for i in reversed(range(5))]

    def test_limit_over_cap_rejected(self, client: TestClient) -> None:
        r = client.get(
            "/api/admin/tokens", params={**AUTH, "limit": 5000}, headers=HEADERS
        )
        assert r.status_code == 422

    def test_malformed_byte_cursor_rejected(self, client: TestClient) -> None:
        r = client.get(
            "/api/admin/tokens", params={**AUTH, "cursor": "junk"}, headers=HEADERS
        )
        assert r.status_code == 422


# ── /api/admin/api-keys — SQL-pushed-down limit + COUNT total ────────────────


@pytest.fixture()
def _tmp_api_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "api-audit.jsonl"
    from novafabric.audit import _paths

    monkeypatch.setattr(_paths, "AUDIT_LOG_PATH", path)
    return path


class TestAdminApiKeysBounding:
    def test_bounded_with_total_and_truncated(
        self, tmp_path: Path, _tmp_api_audit_log: Path
    ) -> None:
        from novafabric.server.api_keys import create_key

        db = tmp_path / "registry.db"
        for i in range(3):
            create_key(f"user{i}@x", ["reader"], actor="seed", db_path=db)
        client = _make_client(tmp_path, db)
        r = client.get(
            "/api/admin/api-keys", params={**AUTH, "limit": 2}, headers=HEADERS
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["keys"]) == 2
        assert body["total"] == 3
        assert body["truncated"] is True

    def test_not_truncated_within_limit(
        self, tmp_path: Path, _tmp_api_audit_log: Path
    ) -> None:
        from novafabric.server.api_keys import create_key

        db = tmp_path / "registry.db"
        create_key("solo@x", ["reader"], actor="seed", db_path=db)
        client = _make_client(tmp_path, db)
        body = client.get(
            "/api/admin/api-keys", params=AUTH, headers=HEADERS
        ).json()
        assert body["total"] == 1
        assert body["truncated"] is False

    def test_limit_over_cap_rejected(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path, tmp_path / "registry.db")
        r = client.get(
            "/api/admin/api-keys", params={**AUTH, "limit": 9999}, headers=HEADERS
        )
        assert r.status_code == 422


# ── /api/assets/{asset_id} — bounded eval history ────────────────────────────


class TestAssetEvalHistoryBounding:
    @pytest.fixture()
    def client(self, tmp_path: Path) -> TestClient:
        db = tmp_path / "registry.db"
        conn = get_connection(db)
        init_schema(conn)
        conn.execute(
            "INSERT INTO assets (id, name, asset_type, version, status, spec_json,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            ("a1", "asset-one", "model", "1.0.0", "development", "{}",
             "2026-07-30T00:00:00Z"),
        )
        for i in range(7):
            conn.execute(
                "INSERT INTO eval_results (id, asset_id, suite_name, passed,"
                " score_json, run_at) VALUES (?,?,?,?,?,?)",
                (f"ev{i}", "a1", "smoke", 1, "{}", f"2026-07-30T00:00:0{i}Z"),
            )
        conn.commit()
        conn.close()
        return _make_client(tmp_path, db)

    def test_bounded_with_total_and_truncated(self, client: TestClient) -> None:
        r = client.get(
            "/api/assets/a1", params={**AUTH, "eval_limit": 3}, headers=HEADERS
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["eval_results"]) == 3
        # newest-first
        assert body["eval_results"][0]["run_at"] == "2026-07-30T00:00:06Z"
        assert body["eval_results_total"] == 7
        assert body["eval_results_truncated"] is True
        assert body["id"] == "a1"  # existing keys preserved

    def test_not_truncated_within_default(self, client: TestClient) -> None:
        body = client.get("/api/assets/a1", params=AUTH, headers=HEADERS).json()
        assert len(body["eval_results"]) == 7
        assert body["eval_results_total"] == 7
        assert body["eval_results_truncated"] is False

    def test_limit_over_cap_rejected(self, client: TestClient) -> None:
        r = client.get(
            "/api/assets/a1", params={**AUTH, "eval_limit": 501}, headers=HEADERS
        )
        assert r.status_code == 422


# ── unit tests: pushed-down store reads ──────────────────────────────────────


class TestReviewQueuePushdown:
    def test_list_pending_limit_and_count(self, tmp_path: Path) -> None:
        from novafabric.kg.review_queue import HumanReviewQueueWriter, ReviewItem

        q = HumanReviewQueueWriter(db_path=tmp_path / "queue.db")
        try:
            for i in range(4):
                q.enqueue(
                    ReviewItem(
                        alias=f"m-{i}", entity_type="model", confidence=0.2
                    )
                )
            assert q.count_pending() == 4
            page = q.list_pending(limit=2)
            assert len(page) == 2
            assert len(q.list_pending()) == 4  # None keeps legacy behavior
            # resolving an item shrinks the pending count
            q.approve(page[0].item_id, canonical="canon", resolved_by="t")
            assert q.count_pending() == 3
        finally:
            q.close()


class TestApiKeysPushdown:
    def test_list_keys_limit_and_count(
        self, tmp_path: Path, _tmp_api_audit_log: Path
    ) -> None:
        from novafabric.server.api_keys import count_keys, create_key, list_keys

        db = tmp_path / "registry.db"
        for i in range(3):
            create_key(f"u{i}@x", ["reader"], actor="seed", db_path=db)
        assert count_keys(db_path=db) == 3
        assert len(list_keys(db_path=db, limit=2)) == 2
        assert len(list_keys(db_path=db)) == 3  # None keeps legacy behavior
