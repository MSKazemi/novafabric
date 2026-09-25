"""Tests for ADR-0208 P3: ``GET /v0/usage/export`` (HTTP chargeback export).

Acceptance criteria (ADR-0208 D2 + P3; spec usage-metering-v0.md):

- same rows / ordering / bytes as ``nova server usage export`` (reuses
  ``server/usage_export.py``: ``chargeback_rows`` + the renderers)
- RBAC by filtering, not 403 (D2): admin and auditor see every workspace;
  any other principal sees only its ADR-0178 membership workspaces; a
  ``workspace=``/``org=`` filter can never widen that view (no leakage);
  unauthenticated requests get the same answer as ``GET /v0/usage``
- ``format=csv`` -> ``text/csv; charset=utf-8; header=present`` (RFC 4180,
  formula-injection-safe cells); ``format=ndjson`` -> ``application/x-ndjson``;
  ``Content-Disposition: attachment; filename="nova-usage-<from>_<to>.<ext>"``
- bad ``from``/``to``/``format``, inverted or > 120-period ranges -> 400
  standard error envelope (same style as ``GET /v0/usage?period=``)
- read-only: the registry is opened ``mode=ro`` — a GET never creates the DB
  or the usage tables and never changes the DB file

Exports are not audited: ADR-0208 audits only reconciliation (a write);
``GET /v0/usage`` and the CLI export are unaudited reads as well.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server import usage, usage_export  # noqa: E402
from novafabric.server.api_keys import create_key  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import RateLimitsConfig, ServerConfig  # noqa: E402
from novafabric.server.usage import METRIC_BYTES, METRIC_CAPSULES, Attribution  # noqa: E402

AUG = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
SEP = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
_DEFAULT = Attribution(workspace="default", org="default", source="default")
_TEAM_A = Attribution(workspace="team-a", org="acme", source="key")
_TEAM_B = Attribution(workspace="team-b", org="globex", source="key")
_HOSTILE = Attribution(workspace="=HYPERLINK(1)", org="+evil", source="key")

_URL = "/v0/usage/export"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _close_anchors() -> Iterator[None]:
    yield
    usage.close_anchors()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage-p3.db"
    # Align the default registry path (api-key auth resolves it env-side).
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path / "capsules"))
    return db


def _config(db: Path, *, insecure: bool = True) -> ServerConfig:
    # Metering middleware off: the export itself must be the only DB reader.
    return ServerConfig(
        db_path=str(db),
        insecure_no_auth=insecure,
        rate_limits=RateLimitsConfig(enabled=False),
    )


def _client(db: Path, *, insecure: bool = True) -> TestClient:
    return TestClient(create_app(_config(db, insecure=insecure)), raise_server_exceptions=False)


def _meter(db: Path, run_id: str, att: Attribution, now: datetime, size: int = 100) -> None:
    usage.record_capsule_upload(
        run_id=run_id, size_bytes=size, attribution=att, actor="t", db_path=db, now=now
    )


def _seed(db: Path) -> None:
    """August (finalized by the first September write) + September (provisional)."""
    _meter(db, "r-a1", _TEAM_A, AUG)
    _meter(db, "r-b1", _TEAM_B, AUG)
    _meter(db, "r-d1", _DEFAULT, AUG)
    _meter(db, "r-a2", _TEAM_A, SEP, size=7)
    _meter(db, "r-h1", _HOSTILE, SEP)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _member_key(db: Path) -> str:
    """A reader holding a workspace membership in team-a only."""
    from novafabric.server import workspace_store

    workspace_store.ensure_default(db_path=db)
    acme = workspace_store.create_org("acme", "Acme", "t", db_path=db)
    ws = workspace_store.create_workspace(acme["id"], "team-a", "Team A", "t", db_path=db)
    globex = workspace_store.create_org("globex", "Globex", "t", db_path=db)
    workspace_store.create_workspace(globex["id"], "team-b", "Team B", "t", db_path=db)
    key, _ = create_key("bob@x", ["reader"], actor="t", db_path=db)
    workspace_store.add_membership("bob@x", "workspace", ws["id"], "reader", "t", db_path=db)
    return key


def _csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text, newline="")))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# Formats and headers
# --------------------------------------------------------------------------- #


class TestFormats:
    def test_csv_matches_cli_renderer_byte_for_byte(self, db_path: Path) -> None:
        _seed(db_path)
        resp = _client(db_path).get(_URL, params={"from": "2026-08", "to": "2026-09"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8; header=present"
        assert resp.headers["content-disposition"] == (
            'attachment; filename="nova-usage-2026-08_2026-09.csv"'
        )
        assert resp.headers["cache-control"] == "no-store"
        expected = usage_export.render(
            usage_export.chargeback_rows("2026-08", "2026-09", db_path=db_path), "csv"
        )
        assert resp.content == expected.encode("utf-8")
        assert resp.text.startswith(",".join(usage_export.COLUMNS) + "\r\n")

    def test_csv_rows_final_and_provisional(self, db_path: Path) -> None:
        _seed(db_path)
        rows = _csv_rows(
            _client(db_path).get(_URL, params={"from": "2026-08", "to": "2026-09"}).text
        )
        aug = {(r["workspace"], r["metric"]): r for r in rows if r["period"] == "2026-08"}
        assert aug[("team-a", METRIC_CAPSULES)]["status"] == "final"
        assert aug[("team-a", METRIC_CAPSULES)]["finalized_at"] != ""
        sep = {(r["workspace"], r["metric"]): r for r in rows if r["period"] == "2026-09"}
        assert sep[("team-a", METRIC_BYTES)]["total"] == "7"
        assert sep[("team-a", METRIC_BYTES)]["status"] == "provisional"
        keys = [(r["period"], r["org"], r["workspace"], r["metric"]) for r in rows]
        assert keys == sorted(keys)

    def test_csv_cells_are_formula_injection_safe(self, db_path: Path) -> None:
        _seed(db_path)
        rows = _csv_rows(_client(db_path).get(_URL, params={"from": "2026-09"}).text)
        hostile = [r for r in rows if "HYPERLINK" in r["workspace"]]
        assert hostile, "hostile workspace row missing"
        for r in hostile:
            assert r["workspace"] == "'=HYPERLINK(1)"
            assert r["org"] == "'+evil"
            assert not r["total"].startswith("'")  # integers never rewritten

    def test_ndjson_raw_values_sorted_keys(self, db_path: Path) -> None:
        _seed(db_path)
        resp = _client(db_path).get(
            _URL, params={"from": "2026-08", "to": "2026-09", "format": "ndjson"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-ndjson"
        assert resp.headers["content-disposition"] == (
            'attachment; filename="nova-usage-2026-08_2026-09.ndjson"'
        )
        lines = resp.text.splitlines()
        objs = [json.loads(line) for line in lines]
        assert all(list(o) == sorted(o) for o in objs)
        assert {o["workspace"] for o in objs} >= {"team-a", "team-b", "=HYPERLINK(1)"}
        expected = usage_export.render(
            usage_export.chargeback_rows("2026-08", "2026-09", db_path=db_path), "ndjson"
        )
        assert resp.text == expected

    def test_default_period_is_current(self, db_path: Path) -> None:
        now = datetime.now(timezone.utc)
        _meter(db_path, "r-now", _DEFAULT, now)
        resp = _client(db_path).get(_URL)
        period = usage.period_for()
        assert resp.status_code == 200
        assert f'filename="nova-usage-{period}_{period}.csv"' in resp.headers["content-disposition"]
        assert {r["period"] for r in _csv_rows(resp.text)} == {period}

    def test_workspace_and_org_filters(self, db_path: Path) -> None:
        _seed(db_path)
        client = _client(db_path)
        by_ws = _csv_rows(client.get(_URL, params={"from": "2026-08", "workspace": "team-b"}).text)
        assert {r["workspace"] for r in by_ws} == {"team-b"}
        by_org = _csv_rows(client.get(_URL, params={"from": "2026-08", "org": "acme"}).text)
        assert {r["org"] for r in by_org} == {"acme"}

    def test_empty_range_is_header_only(self, db_path: Path) -> None:
        _seed(db_path)
        resp = _client(db_path).get(_URL, params={"from": "2020-01"})
        assert resp.status_code == 200
        assert resp.text == ",".join(usage_export.COLUMNS) + "\r\n"

    def test_iter_render_joins_to_render(self) -> None:
        rows = [
            usage_export.ChargebackRow(
                period="2026-09",
                org="o",
                workspace='w,"x"',
                metric=METRIC_CAPSULES,
                total=-3,
                status="provisional",
            )
        ]
        for fmt in ("csv", "ndjson"):
            joined = "".join(usage_export.iter_render(rows, fmt))
            assert joined == usage_export.render(rows, fmt)


# --------------------------------------------------------------------------- #
# RBAC — filtering, not 403
# --------------------------------------------------------------------------- #


class TestRbac:
    def test_auditor_sees_all_workspaces(self, db_path: Path) -> None:
        _seed(db_path)
        key, _ = create_key("aud@x", ["auditor"], actor="t", db_path=db_path)
        resp = _client(db_path).get(_URL, params={"from": "2026-08"}, headers=_bearer(key))
        assert resp.status_code == 200
        assert {r["workspace"] for r in _csv_rows(resp.text)} == {"default", "team-a", "team-b"}

    def test_auditor_reaches_get_usage_privileged_view(self, db_path: Path) -> None:
        # D2 regression: a pure-auditor token was 403'd by require_role(reader)
        # before the handler's admin/auditor branch could run.
        _seed(db_path)
        key, _ = create_key("aud@x", ["auditor"], actor="t", db_path=db_path)
        resp = _client(db_path).get("/v0/usage", params={"period": "2026-08"}, headers=_bearer(key))
        assert resp.status_code == 200
        body = resp.json()
        assert {w["workspace"] for w in body["workspaces"]} == {"default", "team-a", "team-b"}
        assert "drift" in body

    def test_role_less_principal_is_still_forbidden(self, db_path: Path) -> None:
        import asyncio

        from novafabric.server.auth import AuthContext
        from novafabric.server.rbac import _Forbidden
        from novafabric.server.routes.usage import _require_usage_viewer

        with pytest.raises(_Forbidden):
            asyncio.run(_require_usage_viewer(AuthContext(subject="nobody@x", roles=[])))

    def test_admin_key_sees_all_workspaces(self, db_path: Path) -> None:
        _seed(db_path)
        key, _ = create_key("root@x", ["admin"], actor="t", db_path=db_path)
        resp = _client(db_path).get(_URL, params={"from": "2026-08"}, headers=_bearer(key))
        assert {r["workspace"] for r in _csv_rows(resp.text)} == {"default", "team-a", "team-b"}

    def test_member_sees_only_membership_workspaces(self, db_path: Path) -> None:
        _seed(db_path)
        key = _member_key(db_path)
        client = _client(db_path)
        for fmt in ("csv", "ndjson"):
            resp = client.get(
                _URL,
                params={"from": "2026-08", "to": "2026-09", "format": fmt},
                headers=_bearer(key),
            )
            assert resp.status_code == 200  # filtering, not 403 (spec RBAC)
            if fmt == "csv":
                seen = {r["workspace"] for r in _csv_rows(resp.text)}
            else:
                seen = {json.loads(line)["workspace"] for line in resp.text.splitlines()}
            assert seen == {"team-a"}
            for leaked in ("team-b", "default", "HYPERLINK", "globex", "evil"):
                assert leaked not in resp.text

    def test_member_filters_cannot_widen_the_view(self, db_path: Path) -> None:
        _seed(db_path)
        key = _member_key(db_path)
        client = _client(db_path)
        for params in (
            {"from": "2026-08", "workspace": "team-b"},
            {"from": "2026-08", "org": "globex"},
            {"from": "2026-08", "workspace": "default"},
        ):
            resp = client.get(_URL, params=params, headers=_bearer(key))
            assert resp.status_code == 200
            assert resp.text == ",".join(usage_export.COLUMNS) + "\r\n"

    def test_member_view_fails_closed_on_store_error(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path)
        key = _member_key(db_path)

        def _boom(*_args: object, **_kwargs: object) -> list[object]:
            raise RuntimeError("store down")

        monkeypatch.setattr("novafabric.server.routes.usage._membership_rows_read_only", _boom)
        resp = _client(db_path).get(
            _URL, params={"from": "2026-08", "format": "ndjson"}, headers=_bearer(key)
        )
        assert resp.status_code == 200
        assert resp.text == ""

    def test_unauthenticated_matches_get_usage(self, db_path: Path) -> None:
        _seed(db_path)
        client = _client(db_path, insecure=False)
        base = client.get("/v0/usage")
        export = client.get(_URL, params={"from": "2026-08"})
        assert base.status_code in (401, 403)
        assert export.status_code == base.status_code
        assert export.json()["error"]["code"] == base.json()["error"]["code"]
        assert "team-a" not in export.text


# --------------------------------------------------------------------------- #
# Parameter validation
# --------------------------------------------------------------------------- #


class TestValidation:
    @pytest.mark.parametrize(
        ("params", "code"),
        [
            ({"from": "2026-13"}, "invalid_period"),
            ({"from": "2026-8"}, "invalid_period"),
            ({"from": "2026-08", "to": "../etc"}, "invalid_period"),
            ({"from": "2026-08\r\nX-Evil: 1"}, "invalid_period"),
            ({"from": "2026-01\n"}, "invalid_period"),  # `$` matches before a final LF
            ({"from": "2026-08", "to": "2026-09\n"}, "invalid_period"),
            ({"from": "\u0662\u0660\u0662\u0666-01"}, "invalid_period"),  # non-ASCII \\d
            ({"from": "2026-09", "to": "2026-08"}, "invalid_period_range"),
            ({"from": "2000-01", "to": "2010-01"}, "invalid_period_range"),
            ({"from": "2026-08", "format": "xlsx"}, "invalid_format"),
            ({"from": "2026-08", "format": "CSV"}, "invalid_format"),
        ],
    )
    def test_bad_params_are_400(self, db_path: Path, params: dict[str, str], code: str) -> None:
        resp = _client(db_path).get(_URL, params=params)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == code
        assert "content-disposition" not in resp.headers

    def test_bad_period_same_status_as_get_usage(self, db_path: Path) -> None:
        client = _client(db_path)
        assert (
            client.get("/v0/usage", params={"period": "bogus"}).status_code
            == client.get(_URL, params={"from": "bogus"}).status_code
        )

    def test_percent_encoded_lf_period_is_400_not_a_header(self, db_path: Path) -> None:
        client = _client(db_path)
        resp = client.get(f"{_URL}?from=2026-01%0A")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_period"
        assert "content-disposition" not in resp.headers
        assert client.get("/v0/usage?period=2026-01%0A").status_code == 400

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            ("2026-08", "2026-09", "nova-usage-2026-08_2026-09.csv"),
            ("2026-08\n", "2026-09\r\nX: 1", "nova-usage-2026-08_2026-091.csv"),
            ('"2026;/..', "2026", "nova-usage-2026_2026.csv"),
        ],
    )
    def test_filename_is_reduced_to_safe_chars(self, start: str, end: str, expected: str) -> None:
        from novafabric.server.routes import usage as usage_routes

        assert usage_routes._export_filename(start, end, "csv") == expected

    def test_exactly_120_periods_allowed(self, db_path: Path) -> None:
        resp = _client(db_path).get(_URL, params={"from": "2017-01", "to": "2026-12"})
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Read-only guarantee
# --------------------------------------------------------------------------- #


class TestReadOnly:
    def test_export_never_changes_the_db(self, db_path: Path) -> None:
        _seed(db_path)
        usage.close_anchors()
        before = _digest(db_path)
        client = _client(db_path)
        for fmt in ("csv", "ndjson"):
            assert client.get(_URL, params={"from": "2026-08", "format": fmt}).status_code == 200
        assert _digest(db_path) == before

    def test_missing_db_is_not_created(self, db_path: Path) -> None:
        assert not db_path.exists()
        resp = _client(db_path).get(_URL, params={"from": "2026-08"})
        assert resp.status_code == 200
        assert resp.text == ",".join(usage_export.COLUMNS) + "\r\n"
        assert not db_path.exists()

    def test_db_without_usage_tables_is_not_migrated(self, db_path: Path) -> None:
        sqlite3.connect(db_path).close()  # empty registry file, no usage tables
        resp = _client(db_path).get(_URL, params={"from": "2026-08", "format": "ndjson"})
        assert resp.status_code == 200
        assert resp.text == ""
        conn = sqlite3.connect(db_path)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
        assert not any(n.startswith("usage_") for n in names)

    def test_read_only_connection_refuses_writes(self, db_path: Path) -> None:
        _seed(db_path)
        conn = usage_export.open_usage_db_read_only(db_path)
        assert conn is not None
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM usage_ledger")
        finally:
            conn.close()

    @pytest.mark.parametrize("message", ["database is locked", "disk I/O error"])
    def test_read_only_path_raises_on_real_store_errors(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
    ) -> None:
        class _Locked:
            def execute(self, *_a: object) -> object:
                raise sqlite3.OperationalError(message)

            def close(self) -> None:
                return None

        monkeypatch.setattr(usage_export, "open_usage_db_read_only", lambda _db=None: _Locked())
        with pytest.raises(usage_export.UsageStoreUnavailableError):
            usage_export.chargeback_rows("2026-08", "2026-08", db_path=db_path, read_only=True)

    def test_locked_store_is_503_not_an_empty_bill(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path)

        class _Locked:
            def execute(self, *_a: object) -> object:
                raise sqlite3.OperationalError("database is locked")

            def close(self) -> None:
                return None

        monkeypatch.setattr(usage_export, "open_usage_db_read_only", lambda _db=None: _Locked())
        resp = _client(db_path).get(_URL, params={"from": "2026-08"})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "usage_store_unavailable"
        assert "content-disposition" not in resp.headers
        assert str(db_path) not in resp.text

    def test_unopenable_registry_raises(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path)
        usage.close_anchors()

        def _refuse(*_a: object, **_k: object) -> sqlite3.Connection:
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(usage_export.sqlite3, "connect", _refuse)
        with pytest.raises(usage_export.UsageStoreUnavailableError):
            usage_export.open_usage_db_read_only(db_path)

    def test_registry_removed_during_open_reads_as_absent(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sqlite3.connect(db_path).close()

        def _vanish(*_a: object, **_k: object) -> sqlite3.Connection:
            db_path.unlink()
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(usage_export.sqlite3, "connect", _vanish)
        assert usage_export.open_usage_db_read_only(db_path) is None

    def test_member_lookup_does_not_create_registry(self, tmp_path: Path) -> None:
        from novafabric.server.routes import usage as usage_routes

        missing = tmp_path / "absent.db"
        assert usage_routes._member_workspace_slugs("bob@x", missing) == set()
        assert not missing.exists()

    def test_member_lookup_leaves_registry_bytes_unchanged(self, db_path: Path) -> None:
        from novafabric.server.routes import usage as usage_routes

        _member_key(db_path)
        before = _digest(db_path)
        assert usage_routes._member_workspace_slugs("bob@x", db_path) == {"team-a"}
        assert _digest(db_path) == before

    def test_member_lookup_does_not_migrate_bare_registry(self, db_path: Path) -> None:
        from novafabric.server.routes import usage as usage_routes

        sqlite3.connect(db_path).close()
        before = _digest(db_path)
        assert usage_routes._member_workspace_slugs("bob@x", db_path) == set()
        assert _digest(db_path) == before

    def test_member_lookup_expands_org_scope(self, db_path: Path) -> None:
        from novafabric.server import workspace_store
        from novafabric.server.routes import usage as usage_routes

        _member_key(db_path)
        globex = next(
            o for o in workspace_store.list_orgs(db_path=db_path) if o["slug"] == "globex"
        )
        workspace_store.add_membership("bob@x", "org", globex["id"], "reader", "t", db_path=db_path)
        assert usage_routes._member_workspace_slugs("bob@x", db_path) == {"team-a", "team-b"}

    def test_member_lookup_fails_closed_on_locked_registry(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.server.routes import usage as usage_routes

        class _Locked:
            def execute(self, *_a: object) -> object:
                raise sqlite3.OperationalError("database is locked")

            def close(self) -> None:
                return None

        monkeypatch.setattr(usage_routes, "open_usage_db_read_only", lambda _db=None: _Locked())
        assert usage_routes._member_workspace_slugs("bob@x", db_path) == set()

    def test_member_export_never_creates_workspace_tables(self, db_path: Path) -> None:
        """A non-privileged export must not run workspace_store's DDL."""
        _seed(db_path)
        key, _ = create_key("carol@x", ["reader"], actor="t", db_path=db_path)
        resp = _client(db_path).get(_URL, params={"from": "2026-08"}, headers=_bearer(key))
        assert resp.status_code == 200
        assert resp.text == ",".join(usage_export.COLUMNS) + "\r\n"
        conn = sqlite3.connect(db_path)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
        assert "memberships" not in names and "workspaces" not in names

    def test_writable_path_still_raises_unexpected_sql_errors(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Broken:
            def execute(self, *_a: object) -> object:
                raise sqlite3.OperationalError("disk I/O error")

            def close(self) -> None:
                return None

        monkeypatch.setattr(usage, "open_usage_db", lambda _db=None: _Broken())
        with pytest.raises(sqlite3.OperationalError):
            usage_export.chargeback_rows("2026-08", "2026-08", db_path=db_path)
