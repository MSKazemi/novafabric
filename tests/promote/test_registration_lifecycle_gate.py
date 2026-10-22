"""Registration cannot bypass the promotion gates, and rollback evidence is bound to identity.

Attacks (adversarial review, 2026-10-22, area 4c):

1. **A spec that declares ``status: production`` was registered straight into
   production** — no ``promote_direct`` gate, no ``promote`` policy decision, no
   ``asset_status_history`` row, no promotion audit entry. ``nova register``,
   ``nova serve`` ``POST /api/assets`` (``operate`` scope) and ``nova server``
   ``POST /v0/assets`` (writer role) all did it.

2. **Rollback history could be forged by reusing a version number.** v1 was in
   production, then archived; ``unregister`` v1, re-register ``m@1.0.0`` with new
   content, ``rollback --to 1.0.0``: the history said "1.0.0 was in production", so
   the new, never-reviewed artifact was restored without a policy decision.

Pinned here:

* a new asset always enters the registry as ``development``; any other declared
  status is refused (``RegistrationStatusError`` / exit 1 / HTTP 422) and nothing is
  written. Every other status is reached through the gated lifecycle commands.
* registration records a ``register`` history row bound to the asset id and the
  SHA-256 of its spec.
* a history row is evidence only for the registration it was written for: same asset
  id **and** same spec digest (legacy rows without an id: written no earlier than the
  current registration).
* a name@version that was ever in production cannot be registered again.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app as cli_app
from novafabric.policy import NoopEngine
from novafabric.policy._models import PolicyDecision, PolicyInput
from novafabric.registry import service as svc
from novafabric.registry.service import (
    DirectPromotionDeniedError,
    DuplicateAssetError,
    RegistrationStatusError,
    RollbackError,
    VersionReuseError,
    get_asset,
    promote_asset,
    register_asset,
    rollback_asset,
    unregister_asset,
)
from novafabric.registry.store import get_connection, init_schema, spec_digest
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec

runner = CliRunner()
NAME = "fraud-model"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "valid_model.yaml"
GATED = [s.value for s in AssetStatus if s is not AssetStatus.development]


class _AllowAll:
    def evaluate(self, input_: PolicyInput) -> PolicyDecision:
        return PolicyDecision(allow=True, reason="test allow", decision_id="t-allow")


def _spec_text(version: str, status: str = "development", artifact: str | None = None) -> str:
    text = FIXTURE.read_text().replace("1.0.0", version)
    text = text.replace("status: development", f"status: {status}")
    if artifact is not None:
        text = text.replace(f"s3://models/fraud-model/{version}", artifact)
    return text


@dataclass
class Reg:
    db: Path
    tmp: Path

    def register(
        self, version: str, status: str = "development", artifact: str | None = None
    ) -> dict[str, Any]:
        path = self.tmp / f"{uuid.uuid4().hex}.yaml"
        path.write_text(_spec_text(version, status, artifact))
        return register_asset(validate_spec(path), path, db_path=self.db)

    def status(self, version: str) -> str:
        return str(get_asset(NAME, version, db_path=self.db)["status"])

    def artifact(self, version: str) -> str:
        row = get_asset(NAME, version, db_path=self.db)
        return str(json.loads(row["spec_json"])["spec"]["artifact_path"])

    def _conn(self) -> sqlite3.Connection:
        conn = get_connection(self.db)
        init_schema(conn)
        return conn

    def rows(self) -> list[tuple[str, str]]:
        conn = self._conn()
        try:
            return [
                (str(r[0]), str(r[1]))
                for r in conn.execute("SELECT version, status FROM assets WHERE name = ?", (NAME,))
            ]
        finally:
            conn.close()

    def history(self, version: str) -> list[sqlite3.Row]:
        conn = self._conn()
        try:
            return list(
                conn.execute(
                    "SELECT * FROM asset_status_history WHERE asset_name = ? AND version = ? "
                    "ORDER BY id",
                    (NAME, version),
                )
            )
        finally:
            conn.close()


@pytest.fixture
def reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Reg]:
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "registry.db"
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    monkeypatch.setattr("novafabric.registry.service.get_policy_engine", NoopEngine)
    yield Reg(db=db, tmp=tmp_path)


def _allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.registry.service.get_policy_engine", _AllowAll)


def _noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.registry.service.get_policy_engine", NoopEngine)


# ---------------------------------------------------------------------------
# Hole 1 — registration never places an asset in a gated status
# ---------------------------------------------------------------------------


class TestRegistrationStatusService:
    @pytest.mark.parametrize("status", GATED)
    def test_a_declared_gated_status_is_refused(self, reg: Reg, status: str) -> None:
        with pytest.raises(RegistrationStatusError, match="development") as exc_info:
            reg.register("1.0.0", status=status)
        assert status in str(exc_info.value)
        assert "nova promote" in str(exc_info.value)
        assert reg.rows() == []
        assert reg.history("1.0.0") == []

    def test_development_registers_and_records_bound_history(self, reg: Reg) -> None:
        row = reg.register("1.0.0")
        assert row["status"] == "development"
        (h,) = reg.history("1.0.0")
        assert (h["from_status"], h["to_status"], h["via"]) == (None, "development", "register")
        assert h["asset_id"] == row["id"]
        assert h["spec_sha256"] == spec_digest(row["spec_json"])

    def test_promotion_history_is_bound_to_the_asset(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch)
        row = reg.register("1.0.0")
        promote_asset(NAME, "1.0.0", AssetStatus.production, actor="owner", db_path=reg.db)
        prod = [h for h in reg.history("1.0.0") if h["to_status"] == "production"]
        assert len(prod) == 1
        assert prod[0]["asset_id"] == row["id"]
        assert prod[0]["spec_sha256"] == spec_digest(row["spec_json"])


class TestRegistrationStatusCLI:
    def test_nova_register_refuses_status_production(self, reg: Reg) -> None:
        path = reg.tmp / "rogue.yaml"
        path.write_text(_spec_text("1.0.0", "production"))
        res = runner.invoke(cli_app, ["register", str(path)])
        assert isinstance(res.exception, SystemExit), res.exception
        assert res.exit_code == 1, res.output
        assert "development" in res.output
        assert reg.rows() == []

    def test_nova_register_still_registers_development(self, reg: Reg) -> None:
        path = reg.tmp / "ok.yaml"
        path.write_text(_spec_text("1.0.0"))
        res = runner.invoke(cli_app, ["register", str(path)])
        assert res.exit_code == 0, res.output
        assert reg.rows() == [("1.0.0", "development")]


TOKEN = "registration-gate-token-0123456789abcdef"
H = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}


def _serve_client(reg: Reg) -> Any:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    caps = reg.tmp / "caps"
    caps.mkdir(exist_ok=True)
    return TestClient(create_app(token=TOKEN, capsule_dir=caps, db_path=reg.db, static_dir=None))


def _server_client(reg: Reg) -> Any:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.server.app import create_app
    from novafabric.server.config import ServerConfig

    app = create_app(ServerConfig(db_path=str(reg.db), insecure_no_auth=True))
    return TestClient(app, raise_server_exceptions=False)


class TestRegistrationStatusHTTP:
    def test_serve_post_assets_refuses_status_production(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "dash.jsonl"))
        c = _serve_client(reg)
        r = c.post(
            "/api/assets",
            headers=H,
            json={"spec_yaml": _spec_text("1.0.0", "production"), "confirmed": True},
        )
        assert r.status_code == 422, r.text
        assert "development" in r.json()["detail"]
        assert reg.rows() == []
        audit = [json.loads(line) for line in (tmp_path / "dash.jsonl").read_text().splitlines()]
        assert audit[-1]["action"] == "register_asset"
        assert audit[-1]["result"] == "error"

    def test_serve_register_from_yaml_refuses_status_production(
        self, reg: Reg, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "dash.jsonl"))
        c = _serve_client(reg)
        r = c.post(
            "/api/assets/register-from-yaml",
            headers=H,
            json={"spec_yaml": _spec_text("1.0.0", "staging")},
        )
        assert r.json()["ok"] is False, r.text
        assert "development" in r.json()["error"]
        assert reg.rows() == []

    def test_server_post_assets_refuses_status_production(self, reg: Reg) -> None:
        c = _server_client(reg)
        r = c.post("/v0/assets", json={"spec_yaml": _spec_text("1.0.0", "production")})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "registration_status_not_allowed"
        assert reg.rows() == []

    def test_server_post_assets_development_still_works(self, reg: Reg) -> None:
        c = _server_client(reg)
        r = c.post("/v0/assets", json={"spec_yaml": _spec_text("1.0.0")})
        assert r.status_code == 201, r.text
        assert reg.rows() == [("1.0.0", "development")]


# ---------------------------------------------------------------------------
# Hole 2 — rollback evidence is bound to the asset, not to name@version
# ---------------------------------------------------------------------------


@pytest.fixture
def history(reg: Reg, monkeypatch: pytest.MonkeyPatch) -> Reg:
    """v1 then v2 legitimately reached production; v1 is now archived."""
    _allow(monkeypatch)
    reg.register("1.0.0", artifact="s3://good/v1.pt")
    promote_asset(NAME, "1.0.0", AssetStatus.production, actor="owner", db_path=reg.db)
    reg.register("2.0.0", artifact="s3://good/v2.pt")
    promote_asset(NAME, "1.0.0", AssetStatus.archived, actor="owner", db_path=reg.db)
    promote_asset(NAME, "2.0.0", AssetStatus.production, actor="owner", db_path=reg.db)
    _noop(monkeypatch)
    return reg


def _swap_row(
    reg: Reg, version: str, artifact: str | None, created_at: str | None = None
) -> None:
    """Replace an asset row behind the service's back (restore, migration, direct write).

    *artifact* ``None`` keeps the spec byte-for-byte; only the row identity changes.
    """
    conn = get_connection(reg.db)
    try:
        old = conn.execute(
            "SELECT * FROM assets WHERE name = ? AND version = ?", (NAME, version)
        ).fetchone()
        spec_json = old["spec_json"]
        if artifact is not None:
            spec = json.loads(spec_json)
            spec["spec"]["artifact_path"] = artifact
            spec_json = json.dumps(spec)
        conn.execute("DELETE FROM assets WHERE id = ?", (old["id"],))
        conn.execute(
            "INSERT INTO assets (id, name, asset_type, version, status, spec_json, "
            "git_commit_sha, created_at, forced_promotion) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 0)",
            (
                str(uuid.uuid4()), NAME, old["asset_type"], version, "archived",
                spec_json, created_at or svc._now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestVersionReuse:
    def test_the_reported_attack_is_refused_at_registration(self, history: Reg) -> None:
        unregister_asset(NAME, "1.0.0", actor="attacker", db_path=history.db)
        with pytest.raises(VersionReuseError, match="was in production"):
            history.register("1.0.0", artifact="s3://attacker/backdoor.pt")
        assert ("1.0.0", "archived") not in history.rows()
        assert history.status("2.0.0") == "production"

    def test_version_reuse_is_a_duplicate_for_existing_callers(self) -> None:
        assert issubclass(VersionReuseError, DuplicateAssetError)

    def test_a_version_that_never_reached_production_can_be_re_registered(
        self, history: Reg
    ) -> None:
        history.register("3.0.0")
        unregister_asset(NAME, "3.0.0", actor="dev", db_path=history.db)
        row = history.register("3.0.0", artifact="s3://fixed/v3.pt")
        assert row["status"] == "development"

    def test_cli_register_refuses_a_reused_production_version(self, history: Reg) -> None:
        unregister_asset(NAME, "1.0.0", actor="attacker", db_path=history.db)
        path = history.tmp / "reuse.yaml"
        path.write_text(_spec_text("1.0.0", artifact="s3://attacker/backdoor.pt"))
        res = runner.invoke(cli_app, ["register", str(path)])
        assert isinstance(res.exception, SystemExit), res.exception
        assert res.exit_code == 1, res.output
        assert "was in production" in res.output


class TestHistoryIsBoundToIdentity:
    def test_a_swapped_row_is_not_previously_production(self, history: Reg) -> None:
        """Defence in depth: the row was replaced without going through registration."""
        _swap_row(history, "1.0.0", "s3://attacker/backdoor.pt")
        with pytest.raises(DirectPromotionDeniedError):
            rollback_asset(NAME, "1.0.0", actor="attacker", db_path=history.db)
        assert history.status("2.0.0") == "production"
        assert history.artifact("1.0.0") == "s3://attacker/backdoor.pt"
        assert history.status("1.0.0") == "archived"

    def test_a_re_created_row_with_identical_content_is_not_evidence(
        self, history: Reg
    ) -> None:
        """Same spec bytes, new registration: its evals and approvals did not survive the
        unregister, so the old row's production history is not its evidence either."""
        _swap_row(history, "1.0.0", None)
        assert history.artifact("1.0.0") == "s3://good/v1.pt"
        with pytest.raises(DirectPromotionDeniedError):
            rollback_asset(NAME, "1.0.0", actor="x", db_path=history.db)
        assert history.status("2.0.0") == "production"

    def test_auto_rollback_skips_a_swapped_row(self, history: Reg) -> None:
        _swap_row(history, "1.0.0", "s3://attacker/backdoor.pt")
        with pytest.raises(RollbackError, match="No previous production version"):
            rollback_asset(NAME, None, actor="attacker", db_path=history.db)
        assert svc.production_history_versions(NAME, db_path=history.db) == ["2.0.0"]

    def test_edited_spec_content_is_not_previously_production(self, history: Reg) -> None:
        conn = get_connection(history.db)
        row = conn.execute(
            "SELECT id, spec_json FROM assets WHERE name = ? AND version = '1.0.0'", (NAME,)
        ).fetchone()
        spec = json.loads(row["spec_json"])
        spec["spec"]["artifact_path"] = "s3://attacker/backdoor.pt"
        conn.execute(
            "UPDATE assets SET spec_json = ? WHERE id = ?", (json.dumps(spec), row["id"])
        )
        conn.commit()
        conn.close()
        with pytest.raises(DirectPromotionDeniedError):
            rollback_asset(NAME, "1.0.0", actor="attacker", db_path=history.db)

    def test_the_genuine_version_still_rolls_back_ungated(self, history: Reg) -> None:
        result = rollback_asset(NAME, None, actor="sre", db_path=history.db)
        assert result["restored_version"] == "1.0.0"
        assert result["gate"] == "previously_production"
        assert history.artifact("1.0.0") == "s3://good/v1.pt"

    def test_serve_rollback_refuses_a_swapped_row(
        self, history: Reg, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "dash.jsonl"))
        _swap_row(history, "1.0.0", "s3://attacker/backdoor.pt")
        c = _serve_client(history)
        r = c.post(f"/api/assets/{NAME}/rollback", headers=H, json={"confirmed": True})
        assert r.status_code == 409, r.text
        assert history.status("2.0.0") == "production"

    def test_cli_rollback_refuses_a_swapped_row(self, history: Reg) -> None:
        _swap_row(history, "1.0.0", "s3://attacker/backdoor.pt")
        res = runner.invoke(
            cli_app, ["rollback", NAME, "--to", "1.0.0", "--db", str(history.db)]
        )
        assert isinstance(res.exception, SystemExit), res.exception
        assert res.exit_code == 1, res.output
        assert "promote_direct" in res.output
        assert history.status("2.0.0") == "production"


class TestLegacyHistoryRows:
    """Rows written before the identity columns existed carry no asset id."""

    def _legacy_row(self, reg: Reg, version: str, changed_at: str) -> None:
        conn = get_connection(reg.db)
        conn.execute(
            "INSERT INTO asset_status_history (asset_name, version, from_status, to_status, "
            "changed_at, changed_by, via) VALUES (?, ?, 'staging', 'production', ?, 'old', "
            "'promote')",
            (NAME, version, changed_at),
        )
        conn.commit()
        conn.close()

    def test_a_legacy_row_older_than_the_registration_is_not_evidence(
        self, history: Reg
    ) -> None:
        history.register("3.0.0")
        self._legacy_row(history, "3.0.0", "2000-01-01T00:00:00+00:00")
        with pytest.raises(DirectPromotionDeniedError):
            rollback_asset(NAME, "3.0.0", actor="x", db_path=history.db)

    def test_a_legacy_row_written_after_the_registration_is_evidence(
        self, history: Reg
    ) -> None:
        history.register("3.0.0")
        self._legacy_row(history, "3.0.0", "2999-01-01T00:00:00+00:00")
        conn = get_connection(history.db)
        conn.execute(
            "UPDATE assets SET status = 'archived' WHERE name = ? AND version = '3.0.0'",
            (NAME,),
        )
        conn.commit()
        conn.close()
        result = rollback_asset(NAME, "3.0.0", actor="x", db_path=history.db)
        assert result["gate"] == "previously_production"

    def test_an_unparseable_legacy_timestamp_is_not_evidence(self, history: Reg) -> None:
        history.register("3.0.0")
        self._legacy_row(history, "3.0.0", "not-a-date")
        with pytest.raises(DirectPromotionDeniedError):
            rollback_asset(NAME, "3.0.0", actor="x", db_path=history.db)

    def test_upgrading_a_registry_adds_the_identity_columns(self, tmp_path: Path) -> None:
        """A registry whose history table predates the columns gains them additively."""
        from novafabric.registry import store as registry_store

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE asset_status_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "asset_name TEXT NOT NULL, version TEXT NOT NULL, from_status TEXT, "
            "to_status TEXT NOT NULL, changed_at TEXT NOT NULL, changed_by TEXT NOT NULL, "
            "via TEXT NOT NULL);"
            "INSERT INTO asset_status_history (asset_name, version, from_status, to_status, "
            "changed_at, changed_by, via) VALUES ('m', '1.0.0', NULL, 'production', "
            "'2026-01-01T00:00:00+00:00', '', 'backfill');"
        )
        conn.commit()
        conn.close()
        registry_store.reset_schema_memo()
        conn = get_connection(db)
        registry_store.init_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(asset_status_history)")}
        assert {"asset_id", "spec_sha256"} <= cols
        (row,) = conn.execute("SELECT * FROM asset_status_history").fetchall()
        assert row["asset_id"] is None  # existing rows are never rewritten
        conn.close()
