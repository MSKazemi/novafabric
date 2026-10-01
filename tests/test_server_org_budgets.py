# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ADR-0294 D1: per-org usage budgets (warn-then-reject at org scope).

- org usage = sum of the org's workspaces' all-time metered counters;
- soft ⇒ 201 + ``org:<org>/<kind> u/l`` warning part; hard ⇒ 429
  ``quota_exceeded`` with additive ``org`` in details, no Retry-After;
- alerts use ``quota:org:{org}:{kind}`` subjects; audit carries ``org``;
- unknown org slugs refused at startup; absent block ⇒ no org checker.
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

from novafabric.server import api_keys, usage, workspace_store  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import (  # noqa: E402
    OrgQuotaConfig,
    QuotaConfig,
    RateLimitsConfig,
    ServerConfig,
    WorkspaceQuotaConfig,
)
from novafabric.server.quotas import (  # noqa: E402
    QUOTA_WARNING_HEADER,
    OrgQuotaChecker,
    QuotaDecision,
    QuotaViolation,
)


def _config(db_path: Path, quota: QuotaConfig | None) -> ServerConfig:
    return ServerConfig(
        db_path=str(db_path),
        insecure_no_auth=True,
        rate_limits=RateLimitsConfig(enabled=True, quota=quota),
    )


def _capsule_zip(run_id: str, payload_bytes: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("capsule.yaml", f"run_id: {run_id}\nstatus: completed\n")
        if payload_bytes:
            zf.writestr("blob.bin", "x" * payload_bytes)
    return buf.getvalue()


def _upload(
    client: TestClient, run_id: str, key: str | None = None, payload_bytes: int = 0
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.post(
        "/v0/capsules",
        files={
            "capsule": (
                f"{run_id}.zip",
                _capsule_zip(run_id, payload_bytes),
                "application/zip",
            )
        },
        headers=headers,
    )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "orgquota-test.db"
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
    calls: list[dict] = []

    def _capture(**kwargs: object) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr("novafabric.events.alerts.emit_ops_alert", _capture)
    return calls


@pytest.fixture
def acme(db_path: Path) -> dict[str, str]:
    """Org `acme` with workspaces `ml` and `ops`; one writer key bound to each."""
    workspace_store.ensure_default(db_path=db_path)
    org = workspace_store.create_org("acme", "Acme", "test", db_path=db_path)
    workspace_store.create_workspace(org["id"], "ml", "ML", "test", db_path=db_path)
    workspace_store.create_workspace(org["id"], "ops", "Ops", "test", db_path=db_path)
    keys = {}
    for ws in ("ml", "ops"):
        key, _ = api_keys.create_key(
            f"svc-{ws}", ["writer", "admin"], actor="test", workspace=ws, db_path=db_path
        )
        keys[ws] = key
    return keys


class TestConfig:
    def test_hard_below_soft_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"quota\.orgs"):
            OrgQuotaConfig(max_bytes_soft=10, max_bytes_hard=5)

    def test_absent_orgs_block_defaults_empty(self) -> None:
        assert QuotaConfig().orgs == {}

    def test_unknown_org_refused_at_startup(self, db_path: Path, capsule_dir: Path) -> None:
        cfg = _config(db_path, QuotaConfig(orgs={"ghost": OrgQuotaConfig(max_capsules_hard=1)}))
        with pytest.raises(ValueError, match="unknown organization"):
            with TestClient(create_app(cfg)):
                pass

    def test_known_org_starts(self, db_path: Path, capsule_dir: Path) -> None:
        cfg = _config(db_path, QuotaConfig(orgs={"default": OrgQuotaConfig(max_capsules_hard=5)}))
        with TestClient(create_app(cfg)) as client:
            assert client.get("/health").status_code == 200
            assert client.app.state.org_quota_checker is not None  # type: ignore[attr-defined]

    def test_zero_budget_installs_nothing(self, db_path: Path, capsule_dir: Path) -> None:
        client = TestClient(
            create_app(_config(db_path, QuotaConfig(orgs={"default": OrgQuotaConfig()})))
        )
        assert getattr(client.app.state, "org_quota_checker", None) is None


class TestEnforcement:
    def test_org_budget_aggregates_workspaces(
        self,
        db_path: Path,
        capsule_dir: Path,
        audit_file: Path,
        alerts: list[dict],
        acme: dict[str, str],
    ) -> None:
        cfg = _config(
            db_path,
            QuotaConfig(orgs={"acme": OrgQuotaConfig(max_capsules_soft=1, max_capsules_hard=2)}),
        )
        client = TestClient(create_app(cfg), raise_server_exceptions=False)
        assert _upload(client, "a1", acme["ml"]).status_code == 201
        warned = _upload(client, "a2", acme["ops"])  # org usage 1 >= soft 1
        assert warned.status_code == 201
        assert warned.headers[QUOTA_WARNING_HEADER] == "org:acme/capsules 1/1"
        resp = _upload(client, "a3", acme["ml"])  # org usage 2 >= hard 2
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["code"] == "quota_exceeded"
        assert body["error"]["details"] == {
            "kind": "capsules",
            "usage": 2,
            "limit": 2,
            "org": "acme",
        }
        assert "Retry-After" not in resp.headers
        assert not (capsule_dir / "a3").exists()
        critical = [a for a in alerts if a.get("severity") == "critical"]
        assert [a["subject_ref"] for a in critical] == ["quota:org:acme:capsules"]
        assert critical[0]["payload"]["org"] == "acme"
        soft = [a for a in alerts if a.get("severity") == "warning"]
        assert [a["subject_ref"] for a in soft] == ["quota:org:acme:capsules:soft"]
        text = audit_file.read_text()
        assert '"org":"acme"' in text

    def test_other_org_unaffected(
        self, db_path: Path, capsule_dir: Path, audit_file: Path, alerts: list[dict],
        acme: dict[str, str],
    ) -> None:
        cfg = _config(db_path, QuotaConfig(orgs={"acme": OrgQuotaConfig(max_capsules_hard=1)}))
        client = TestClient(create_app(cfg), raise_server_exceptions=False)
        assert _upload(client, "b1", acme["ml"]).status_code == 201
        assert _upload(client, "b2", acme["ops"]).status_code == 429
        # Unbound local admin attributes to the default workspace/org.
        assert _upload(client, "b3").status_code == 201

    def test_org_and_workspace_compose_strictest_wins(
        self, db_path: Path, capsule_dir: Path, audit_file: Path, alerts: list[dict],
        acme: dict[str, str],
    ) -> None:
        quota = QuotaConfig(
            workspaces={"ml": WorkspaceQuotaConfig(max_capsules_soft=1)},
            orgs={"acme": OrgQuotaConfig(max_capsules_hard=2)},
        )
        client = TestClient(create_app(_config(db_path, quota)), raise_server_exceptions=False)
        assert _upload(client, "c1", acme["ml"]).status_code == 201
        resp = _upload(client, "c2", acme["ml"])
        assert resp.status_code == 201
        assert resp.headers[QUOTA_WARNING_HEADER] == "ml/capsules 1/1"
        resp = _upload(client, "c3", acme["ml"])
        assert resp.status_code == 429
        assert resp.json()["error"]["details"]["org"] == "acme"
        assert "workspace" not in resp.json()["error"]["details"]

    def test_delete_reclaims_org_budget(
        self, db_path: Path, capsule_dir: Path, audit_file: Path, alerts: list[dict],
        acme: dict[str, str],
    ) -> None:
        cfg = _config(db_path, QuotaConfig(orgs={"acme": OrgQuotaConfig(max_capsules_hard=1)}))
        client = TestClient(create_app(cfg), raise_server_exceptions=False)
        assert _upload(client, "d1", acme["ml"]).status_code == 201
        assert _upload(client, "d2", acme["ml"]).status_code == 429
        assert client.delete(
            "/v0/capsules/d1", headers={"Authorization": f"Bearer {acme['ml']}"}
        ).status_code == 200
        assert _upload(client, "d3", acme["ml"]).status_code == 201


class TestUnit:
    def test_org_totals_sum_mapped_workspaces(self, db_path: Path, acme: dict[str, str]) -> None:
        for ws, org, n in (("ml", "acme", 2), ("ops", "acme", 3), ("default", "default", 7)):
            usage.record_entries(
                [
                    usage.LedgerEntry(
                        metric=usage.METRIC_CAPSULES, amount=n, ref=f"{ws}-c",
                        workspace=ws, org=org, attribution="key", actor="t",
                    ),
                    usage.LedgerEntry(
                        metric=usage.METRIC_BYTES, amount=n * 10, ref=f"{ws}-b",
                        workspace=ws, org=org, attribution="key", actor="t",
                    ),
                ],
                db_path=db_path,
            )
        assert usage.org_all_time_totals("acme", db_path=db_path) == (5, 50)
        assert usage.org_all_time_totals("default", db_path=db_path) == (7, 70)
        assert usage.org_all_time_totals("nobody", db_path=db_path) == (0, 0)

    def test_unknown_workspace_counts_toward_default_org(self, db_path: Path) -> None:
        workspace_store.ensure_default(db_path=db_path)
        usage.record_entries(
            [
                usage.LedgerEntry(
                    metric=usage.METRIC_CAPSULES, amount=1, ref="ghost-c",
                    workspace="ghost", org="default", attribution="key", actor="t",
                )
            ],
            db_path=db_path,
        )
        assert usage.org_all_time_totals("default", db_path=db_path)[0] == 1

    def test_reader_caches_within_ttl(self, db_path: Path) -> None:
        now = [0.0]
        reader = usage.OrgUsageReader(db_path, clock=lambda: now[0])
        assert reader.get("default") == (0, 0)
        usage.record_entries(
            [
                usage.LedgerEntry(
                    metric=usage.METRIC_CAPSULES, amount=4, ref="x",
                    workspace="default", org="default", attribution="default", actor="t",
                )
            ],
            db_path=db_path,
        )
        assert reader.get("default") == (0, 0)  # cached
        now[0] = 10.0
        assert reader.get("default") == (4, 0)

    def test_unbudgeted_org_never_reads_usage(self) -> None:
        reads: list[str] = []

        def reader(org: str) -> tuple[int, int]:
            reads.append(org)
            return (0, 0)

        checker = OrgQuotaChecker({"acme": OrgQuotaConfig(max_capsules_hard=1)}, reader)
        assert checker.check("other").outcome == "ok"
        assert reads == []

    def test_violation_labels(self) -> None:
        decision = QuotaDecision(
            outcome="warn",
            violations=(
                QuotaViolation(kind="capsules", usage=1, limit=1, severity="soft"),
                QuotaViolation(
                    kind="bytes", usage=2, limit=2, severity="soft", workspace="ml"
                ),
                QuotaViolation(kind="bytes", usage=3, limit=3, severity="soft", org="acme"),
            ),
        )
        assert decision.warning_header == (
            "capsules 1/1, ml/bytes 2/2, org:acme/bytes 3/3"
        )
