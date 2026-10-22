"""ADR-0312 D1 on the HTTP surface: dashboard promote = ``nova promote direct``.

``nova promote direct`` refuses a protected stage (``production``) unless the
``promote_direct`` policy action explicitly allows it, and ``--force`` does not
bypass that. The dashboard's ``POST /api/assets/{name}/{version}/promote`` (and the
by-id alias) called ``promote_asset`` directly, so an ``operate``-scoped token
reached ``production`` single-actor -- the gap ADR-0312's "Limits" section names.

These tests drive the CLI and the HTTP route through the same scenarios with the
same policy engine and assert they agree: refused exactly where the CLI is
refused, allowed exactly where the CLI is allowed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from novafabric.cli.main import app as cli_app  # noqa: E402
from novafabric.policy import NoopEngine  # noqa: E402
from novafabric.policy._models import PolicyDecision, PolicyInput  # noqa: E402
from novafabric.registry.service import get_asset, promote_asset, register_asset  # noqa: E402
from novafabric.serve import audit  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.spec.models import AssetStatus  # noqa: E402
from novafabric.spec.validator import validate_spec  # noqa: E402

TOKEN = "http-promote-gate-token-0123456789"
H = {"host": "127.0.0.1:4321"}
NAME, VERSION = "fraud-model", "1.0.0"
runner = CliRunner()


class _AllowAll:
    """An OPA stand-in whose policy explicitly allows every action."""

    def __init__(self) -> None:
        self.inputs: list[PolicyInput] = []

    def evaluate(self, input_: PolicyInput) -> PolicyDecision:
        self.inputs.append(input_)
        return PolicyDecision(allow=True, reason="test allow", decision_id="t-allow")


class _DenyDirect:
    """Allows everything except ``promote_direct``."""

    def __init__(self) -> None:
        self.inputs: list[PolicyInput] = []

    def evaluate(self, input_: PolicyInput) -> PolicyDecision:
        self.inputs.append(input_)
        return PolicyDecision(
            allow=input_.action != "promote_direct", reason="deny direct", decision_id="t-deny"
        )


_ENGINES: dict[str, Any] = {"noop": NoopEngine, "allow": _AllowAll, "deny": _DenyDirect}


@dataclass
class Env:
    db: Path
    tmp: Path

    def status(self) -> str:
        return str(get_asset(NAME, VERSION, db_path=self.db)["status"])


def _fresh_registry(tmp: Path, fixtures_dir: Path, label: str) -> Path:
    db = tmp / f"{label}.db"
    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=db)
    promote_asset(NAME, VERSION, AssetStatus.validated, actor="ci", db_path=db)
    return db


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv(audit.AUDIT_ENV, str(tmp_path / "dashboard-audit.jsonl"))
    yield tmp_path


def _use_engine(monkeypatch: pytest.MonkeyPatch, kind: str) -> Any:
    engine = _ENGINES[kind]()
    monkeypatch.setattr("novafabric.registry.service.get_policy_engine", lambda: engine)
    return engine


def _cli_promote(
    monkeypatch: pytest.MonkeyPatch, db: Path, to: str, force: bool
) -> bool:
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    args = ["promote", "direct", f"{NAME}@{VERSION}", "--to", to]
    if force:
        args.append("--force")
    res = runner.invoke(cli_app, args, input=f"{NAME}\n" if force else None)
    assert res.exit_code in (0, 1), res.output
    return res.exit_code == 0


def _http_promote(db: Path, capsules: Path, to: str, force: bool, *, by_id: bool = False) -> Any:
    app = create_app(token=TOKEN, capsule_dir=capsules, db_path=db, static_dir=None)
    with TestClient(app) as c:
        if by_id:
            asset_id = get_asset(NAME, VERSION, db_path=db)["id"]
            url = f"/api/assets/{asset_id}/promote"
        else:
            url = f"/api/assets/{NAME}/{VERSION}/promote"
        return c.post(
            url,
            params={"token": TOKEN},
            headers=H,
            json={"to_status": to, "confirmed": True, "force": force},
        )


@pytest.mark.parametrize("engine", ["noop", "allow", "deny"])
@pytest.mark.parametrize("to", ["staging", "production"])
@pytest.mark.parametrize("force", [False, True])
def test_http_promote_agrees_with_the_cli(
    isolated: Path,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    to: str,
    force: bool,
) -> None:
    capsules = isolated / "runs"
    capsules.mkdir()
    _use_engine(monkeypatch, engine)

    cli_db = _fresh_registry(isolated, fixtures_dir, "cli")
    cli_allowed = _cli_promote(monkeypatch, cli_db, to, force)

    http_db = _fresh_registry(isolated, fixtures_dir, "http")
    res = _http_promote(http_db, capsules, to, force)
    http_allowed = res.status_code == 200

    # ADR-0312 D1: production only with an explicit (non-Noop) policy allow;
    # --force never bypasses it; other stages are unaffected.
    expected = to != "production" or engine == "allow"
    assert cli_allowed is expected, "the CLI baseline changed"
    assert http_allowed is cli_allowed, (
        f"HTTP {'allowed' if http_allowed else 'refused'} what the CLI "
        f"{'allowed' if cli_allowed else 'refused'}: {res.status_code} {res.text}"
    )
    assert Env(http_db, isolated).status() == (to if expected else "validated")
    if not expected:
        assert res.status_code == 403, res.text
        assert "promote propose" in res.json()["detail"]


def test_the_by_id_alias_is_gated_too(
    isolated: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsules = isolated / "runs"
    capsules.mkdir()
    _use_engine(monkeypatch, "noop")
    db = _fresh_registry(isolated, fixtures_dir, "byid")
    res = _http_promote(db, capsules, "production", True, by_id=True)
    assert res.status_code == 403, res.text
    assert Env(db, isolated).status() == "validated"


def test_a_refusal_is_audited_on_the_dashboard_trail(
    isolated: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsules = isolated / "runs"
    capsules.mkdir()
    _use_engine(monkeypatch, "noop")
    db = _fresh_registry(isolated, fixtures_dir, "audit")
    _http_promote(db, capsules, "production", False)
    entries = [e for e in audit.read_recent(20) if e["action"] == "promote_asset"]
    assert entries and entries[0]["result"] == "error"
    assert "protected stage" in entries[0]["error"]


def test_the_policy_subject_is_the_authenticated_credential_not_the_body(
    isolated: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``actor`` in the body is caller-chosen; a policy must not be able to key on it."""
    from novafabric.serve.auth import token_fingerprint

    capsules = isolated / "runs"
    capsules.mkdir()
    engine = _use_engine(monkeypatch, "allow")
    db = _fresh_registry(isolated, fixtures_dir, "subject")
    app = create_app(token=TOKEN, capsule_dir=capsules, db_path=db, static_dir=None)
    with TestClient(app) as c:
        res = c.post(
            f"/api/assets/{NAME}/{VERSION}/promote",
            params={"token": TOKEN},
            headers=H,
            json={"to_status": "production", "confirmed": True, "actor": "trusted-release-bot"},
        )
    assert res.status_code == 200, res.text
    direct = [i for i in engine.inputs if i.action == "promote_direct"]
    assert len(direct) == 1
    assert direct[0].subject.user == f"serve:{token_fingerprint(TOKEN)}"
    assert direct[0].resource.to_status == "production"
    # The body's actor is still what the registry records as promoted_by.
    assert get_asset(NAME, VERSION, db_path=db)["promoted_by"] == "trusted-release-bot"
