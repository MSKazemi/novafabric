"""Tests for ADR-0208 D3 per-workspace quota budgets (warn-then-reject).

Contract: design/spec/usage-metering-v0.md §Quota enforcement / §Alerts
  - same ladder and 429 contract as the global ADR-0179 path; enforcement
    reads the all-time METERED counters (never the FS walk)
  - soft => 201 + `X-NovaFabric-Quota-Warning: <workspace>/<kind> u/l` +
    one warning-severity ops.quota.breached per (workspace, kind) window
  - hard => 429 quota_exceeded with additive `workspace` in details, no
    Retry-After, + critical ops.quota.breached subject `quota:{ws}:{kind}`
  - 0 = unlimited; absent `workspaces` block => byte-identical global path
  - config naming an unknown workspace slug is refused at startup
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import (  # noqa: E402
    QuotaConfig,
    RateLimitsConfig,
    ServerConfig,
    WorkspaceQuotaConfig,
)
from novafabric.server.quotas import (  # noqa: E402
    QUOTA_WARNING_HEADER,
    QuotaViolation,
    WorkspaceQuotaChecker,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _client(cfg: ServerConfig) -> TestClient:
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _config(db_path: Path, quota: QuotaConfig | None) -> ServerConfig:
    return ServerConfig(
        db_path=str(db_path),
        insecure_no_auth=True,
        rate_limits=RateLimitsConfig(enabled=True, quota=quota),
    )


def _ws_quota(**kwargs: int) -> QuotaConfig:
    return QuotaConfig(workspaces={"default": WorkspaceQuotaConfig(**kwargs)})


def _capsule_zip(run_id: str, payload_bytes: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("capsule.yaml", f"run_id: {run_id}\nstatus: completed\n")
        if payload_bytes:
            zf.writestr("blob.bin", "x" * payload_bytes)
    return buf.getvalue()


def _upload(client: TestClient, run_id: str, payload_bytes: int = 0) -> httpx.Response:
    return client.post(
        "/v0/capsules",
        files={
            "capsule": (
                f"{run_id}.zip",
                _capsule_zip(run_id, payload_bytes),
                "application/zip",
            )
        },
    )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "wsquota-test.db"
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    return db


@pytest.fixture
def capsule_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cdir = tmp_path / "capsules"
    cdir.mkdir()
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(cdir))
    return cdir


@pytest.fixture
def audit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(path))
    return path


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture emit_ops_alert calls (the lazy import binds at call time)."""
    calls: list[dict] = []

    def _capture(**kwargs: object) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr("novafabric.events.alerts.emit_ops_alert", _capture)
    return calls


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #


class TestConfig:
    def test_workspace_hard_below_soft_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_capsules_hard"):
            WorkspaceQuotaConfig(max_capsules_soft=10, max_capsules_hard=5)

    def test_absent_workspaces_block_defaults_empty(self) -> None:
        assert QuotaConfig().workspaces == {}

    def test_unknown_workspace_slug_refused_at_startup(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        cfg = _config(
            db_path,
            QuotaConfig(
                workspaces={"ghost": WorkspaceQuotaConfig(max_capsules_hard=1)}
            ),
        )
        app = create_app(cfg)
        with pytest.raises(ValueError, match="unknown workspace"):
            with TestClient(app):
                pass  # startup (lifespan) must refuse

    def test_known_workspace_slug_starts_cleanly(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        # `default` exists after the ADR-0178 bootstrap — startup succeeds.
        cfg = _config(db_path, _ws_quota(max_capsules_hard=100))
        with TestClient(create_app(cfg)) as client:
            assert client.get("/health").status_code == 200


# --------------------------------------------------------------------------- #
# Enforcement ladder (route level, metered counters)
# --------------------------------------------------------------------------- #


class TestSoftWarn:
    def test_soft_threshold_warns_with_workspace_header(
        self, db_path: Path, capsule_dir: Path, audit_file: Path, alerts: list[dict]
    ) -> None:
        client = _client(_config(db_path, _ws_quota(max_capsules_soft=1)))
        assert _upload(client, "w1").status_code == 201  # 0 < soft → clean
        resp = _upload(client, "w2")  # metered usage 1 >= soft 1 → warn
        assert resp.status_code == 201
        assert resp.headers[QUOTA_WARNING_HEADER] == "default/capsules 1/1"
        assert (capsule_dir / "w2" / "capsule.yaml").exists()

    def test_soft_alert_emitted_once_per_window(
        self, db_path: Path, capsule_dir: Path, audit_file: Path, alerts: list[dict]
    ) -> None:
        client = _client(_config(db_path, _ws_quota(max_capsules_soft=1)))
        assert _upload(client, "w1").status_code == 201
        assert _upload(client, "w2").status_code == 201  # first warn → alert
        assert _upload(client, "w3").status_code == 201  # same window → dedup
        soft = [a for a in alerts if a.get("severity") == "warning"]
        assert len(soft) == 1
        assert soft[0]["event_type"] == "ops.quota.breached"
        assert soft[0]["subject_ref"] == "quota:default:capsules:soft"
        assert soft[0]["payload"] == {
            "workspace": "default",
            "kind": "capsules",
            "usage": 1,
            "limit": 1,
        }
        # One audit record per (workspace, kind) per window, workspace field set.
        text = audit_file.read_text()
        assert text.count('"quota_soft_exceeded"') >= 1
        assert '"workspace":"default"' in text


class TestHardReject:
    def test_429_envelope_carries_workspace_and_fires_critical_alert(
        self, db_path: Path, capsule_dir: Path, audit_file: Path, alerts: list[dict]
    ) -> None:
        client = _client(
            _client_cfg := _config(
                db_path, _ws_quota(max_capsules_soft=1, max_capsules_hard=2)
            )
        )
        assert _upload(client, "h1").status_code == 201
        assert _upload(client, "h2").status_code == 201  # warned, still writes
        resp = _upload(client, "h3")  # metered usage 2 >= hard 2 → reject
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["code"] == "quota_exceeded"
        assert body["error"]["details"] == {
            "kind": "capsules",
            "usage": 2,
            "limit": 2,
            "workspace": "default",
        }
        assert "Retry-After" not in resp.headers
        assert not (capsule_dir / "h3").exists()  # store untouched
        # Repeated rejection inside the window: one critical alert only.
        assert _upload(client, "h4").status_code == 429
        critical = [a for a in alerts if a.get("severity") == "critical"]
        assert len(critical) == 1
        assert critical[0]["subject_ref"] == "quota:default:capsules"
        assert critical[0]["payload"]["workspace"] == "default"
        assert _client_cfg.rate_limits.quota is not None  # config untouched

    def test_bytes_budget_rejects_on_metered_bytes(
        self, db_path: Path, capsule_dir: Path, audit_file: Path, alerts: list[dict]
    ) -> None:
        client = _client(_config(db_path, _ws_quota(max_bytes_hard=50)))
        assert _upload(client, "b1", payload_bytes=500).status_code == 201
        resp = _upload(client, "b2")
        assert resp.status_code == 429
        details = resp.json()["error"]["details"]
        assert details["kind"] == "bytes"
        assert details["limit"] == 50
        assert details["usage"] >= 500
        assert details["workspace"] == "default"

    def test_deletion_reclaims_budget(
        self, db_path: Path, capsule_dir: Path, audit_file: Path, alerts: list[dict]
    ) -> None:
        # ADR-0208 D3 + ADR-0206 compose: negative delete rows free budget.
        client = _client(_config(db_path, _ws_quota(max_capsules_hard=1)))
        assert _upload(client, "c1").status_code == 201
        assert _upload(client, "c2").status_code == 429  # budget consumed
        assert client.delete("/v0/capsules/c1").status_code == 200
        assert _upload(client, "c3").status_code == 201  # reclaimed


class TestUnlimitedAndInert:
    def test_zero_limits_workspace_is_unlimited(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        client = _client(_config(db_path, _ws_quota()))  # all-zero budget
        assert getattr(client.app.state, "workspace_quota_checker", None) is None
        assert _upload(client, "z1").status_code == 201

    def test_absent_workspaces_block_keeps_global_details_shape(
        self, db_path: Path, capsule_dir: Path, audit_file: Path
    ) -> None:
        # Contract pin against the existing ADR-0179 tests: a global-only
        # quota block must produce the exact pre-0208 details (no workspace).
        for rid in ("g-seed",):
            dest = capsule_dir / rid
            dest.mkdir()
            (dest / "capsule.yaml").write_text(f"run_id: {rid}\n")
        client = _client(_config(db_path, QuotaConfig(max_capsules_hard=1)))
        assert getattr(client.app.state, "workspace_quota_checker", None) is None
        resp = _upload(client, "g1")
        assert resp.status_code == 429
        assert resp.json()["error"]["details"] == {
            "kind": "capsules",
            "usage": 1,
            "limit": 1,
        }

    def test_global_and_workspace_checks_compose(
        self, db_path: Path, capsule_dir: Path, audit_file: Path, alerts: list[dict]
    ) -> None:
        # Both configured: global soft warns while the workspace stays clean;
        # header carries the global part (global parts first).
        quota = QuotaConfig(
            max_capsules_soft=1,
            workspaces={"default": WorkspaceQuotaConfig(max_capsules_soft=100)},
        )
        dest = capsule_dir / "seed"
        dest.mkdir()
        (dest / "capsule.yaml").write_text("run_id: seed\n")
        client = _client(_config(db_path, quota))
        resp = _upload(client, "gw1")
        assert resp.status_code == 201
        assert resp.headers[QUOTA_WARNING_HEADER] == "capsules 1/1"


# --------------------------------------------------------------------------- #
# Checker unit semantics
# --------------------------------------------------------------------------- #


class TestWorkspaceCheckerUnit:
    def _checker(
        self,
        budgets: dict[str, WorkspaceQuotaConfig],
        usage_by_ws: dict[str, tuple[int, int]],
        **kwargs: object,
    ) -> tuple[WorkspaceQuotaChecker, list[QuotaViolation], list[QuotaViolation]]:
        hard_alerts: list[QuotaViolation] = []
        soft_alerts: list[QuotaViolation] = []
        checker = WorkspaceQuotaChecker(
            budgets,
            lambda ws: usage_by_ws.get(ws, (0, 0)),
            audit_hook=lambda _p: None,
            alert_hook=hard_alerts.append,
            soft_alert_hook=soft_alerts.append,
            **kwargs,  # type: ignore[arg-type]
        )
        return checker, hard_alerts, soft_alerts

    def test_unbudgeted_workspace_never_reads_usage(self) -> None:
        reads: list[str] = []

        def reader(ws: str) -> tuple[int, int]:
            reads.append(ws)
            return (0, 0)

        checker = WorkspaceQuotaChecker(
            {"team-a": WorkspaceQuotaConfig(max_capsules_hard=1)}, reader
        )
        assert checker.check("other").outcome == "ok"
        assert reads == []

    def test_hard_wins_and_violation_carries_workspace(self) -> None:
        checker, hard_alerts, _ = self._checker(
            {"team-a": WorkspaceQuotaConfig(max_capsules_soft=1, max_bytes_hard=10)},
            {"team-a": (5, 50)},
        )
        decision = checker.check("team-a")
        assert decision.outcome == "reject"
        hard = decision.hard
        assert hard is not None
        assert (hard.kind, hard.usage, hard.limit, hard.workspace) == (
            "bytes",
            50,
            10,
            "team-a",
        )
        assert len(hard_alerts) == 1

    def test_warning_header_prefixes_workspace(self) -> None:
        checker, _, soft_alerts = self._checker(
            {"team-a": WorkspaceQuotaConfig(max_capsules_soft=5)},
            {"team-a": (5, 0)},
        )
        decision = checker.check("team-a")
        assert decision.outcome == "warn"
        assert decision.warning_header == "team-a/capsules 5/5"
        assert len(soft_alerts) == 1

    def test_audit_window_bounds_alerts_per_workspace(self) -> None:
        now = [0.0]
        checker, hard_alerts, _ = self._checker(
            {
                "a": WorkspaceQuotaConfig(max_capsules_hard=1),
                "b": WorkspaceQuotaConfig(max_capsules_hard=1),
            },
            {"a": (1, 0), "b": (1, 0)},
            clock=lambda: now[0],
            audit_window_seconds=10.0,
        )
        for _ in range(3):
            assert checker.check("a").outcome == "reject"
        # Per-workspace subjects: b's breach is NOT suppressed by a's window.
        assert checker.check("b").outcome == "reject"
        assert [v.workspace for v in hard_alerts] == ["a", "b"]
        now[0] += 11.0
        assert checker.check("a").outcome == "reject"
        assert [v.workspace for v in hard_alerts] == ["a", "b", "a"]
