"""Tests for the ADR-0182 self-observability surface on the server app.

Contract: design/spec/ops-observability-surface-v0.md
  - /livez    — liveness only, never checks dependencies
  - /readyz   — itemized checks, 503 + named failing check when degraded
  - /v0/version — structured identity, reader-role-gated
  - /metrics  — Prometheus exposition, gated by default, operator exemption
  - normative cardinality/privacy rules (route template only, no raw paths)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import (  # noqa: E402
    ObservabilityConfig,
    OidcConfig,
    ServerConfig,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _client(cfg: ServerConfig) -> TestClient:
    return TestClient(create_app(cfg), raise_server_exceptions=False)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "obs-test.db"


@pytest.fixture
def client(db_path: Path) -> TestClient:
    """Anonymous-admin mode via the explicit ADR-0184 opt-out."""
    return _client(ServerConfig(db_path=str(db_path), insecure_no_auth=True))


@pytest.fixture
def oidc_client(db_path: Path) -> TestClient:
    """OIDC-enabled mode — unauthenticated requests must be rejected."""
    cfg = ServerConfig(
        db_path=str(db_path),
        oidc=OidcConfig(issuer_url="https://issuer.example", audience="nova"),
    )
    return _client(cfg)


# --------------------------------------------------------------------------- #
# /livez
# --------------------------------------------------------------------------- #


class TestLivez:
    def test_livez_returns_200_ok(self, client: TestClient) -> None:
        resp = client.get("/livez")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_livez_never_checks_dependencies(self, tmp_path: Path) -> None:
        # Broken DB path: /readyz would fail, but liveness must still be 200.
        broken = tmp_path / "no-such-dir" / "x.db"
        c = _client(ServerConfig(db_path=str(broken)))
        resp = c.get("/livez")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_livez_unauthenticated_even_with_oidc(self, oidc_client: TestClient) -> None:
        assert oidc_client.get("/livez").status_code == 200


# --------------------------------------------------------------------------- #
# /readyz
# --------------------------------------------------------------------------- #


class TestReadyz:
    def test_readyz_ok(self, client: TestClient) -> None:
        resp = client.get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["checks"]["db"] == "ok"
        # Fresh registry DB is not alembic-stamped — honest "unknown", never a
        # fake "ok"; and unknown must not fail readiness.
        assert body["checks"]["migrations"] in ("ok", "unknown")
        # No object store configured on the server app — spec: skipped.
        assert body["checks"]["object_store"] == "skipped"

    def test_readyz_degraded_on_broken_db(self, tmp_path: Path) -> None:
        broken = tmp_path / "no-such-dir" / "x.db"
        c = _client(ServerConfig(db_path=str(broken)))
        resp = c.get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["db"] == "fail"  # the failing check is named

    def test_readyz_body_contains_only_check_names_and_statuses(
        self, tmp_path: Path
    ) -> None:
        # Normative: never DSNs, paths, or error strings in the /readyz body.
        broken = tmp_path / "no-such-dir" / "secret-name.db"
        c = _client(ServerConfig(db_path=str(broken)))
        text = c.get("/readyz").text
        assert "secret-name" not in text
        assert str(tmp_path) not in text


# --------------------------------------------------------------------------- #
# /v0/version
# --------------------------------------------------------------------------- #


class TestVersion:
    def test_version_payload_fields(self, client: TestClient) -> None:
        resp = client.get("/v0/version")
        assert resp.status_code == 200
        body = resp.json()
        for field in ("version", "git_sha", "schema_revision", "extras", "features"):
            assert field in body, f"missing field: {field}"
        assert isinstance(body["version"], str) and body["version"]
        assert isinstance(body["git_sha"], str) and body["git_sha"]
        assert isinstance(body["schema_revision"], str)
        assert isinstance(body["extras"], list)
        # ADR-0182 D5: self-tracing is opt-in and default OFF.
        assert body["features"]["self_tracing"] is False

    def test_version_git_sha_from_env(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_BUILD_SHA", "cafebabe1234567")
        body = client.get("/v0/version").json()
        assert body["git_sha"] == "cafebabe1234567"

    def test_version_role_gated(self, oidc_client: TestClient) -> None:
        # Reconnaissance data: unauthenticated request must be rejected.
        assert oidc_client.get("/v0/version").status_code == 401


# --------------------------------------------------------------------------- #
# /metrics
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_metrics_content_type_is_prometheus_text(self, client: TestClient) -> None:
        pytest.importorskip("prometheus_client")
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")

    def test_metrics_counter_increments_by_route_template(
        self, client: TestClient
    ) -> None:
        pytest.importorskip("prometheus_client")
        client.get("/livez")
        client.get("/livez")
        text = client.get("/metrics").text
        # prometheus-client renders label names in sorted order:
        # app, method, route, status.
        match = re.search(
            r'nova_http_requests_total\{app="server",method="GET",'
            r'route="/livez",status="200"\}\s+(\d+(?:\.\d+)?)',
            text,
        )
        assert match is not None, f"livez counter sample missing:\n{text}"
        assert float(match.group(1)) >= 2
        assert "nova_http_request_duration_seconds" in text

    def test_metrics_gated_by_default(self, oidc_client: TestClient) -> None:
        assert oidc_client.get("/metrics").status_code == 401

    def test_db_pool_gauge_sampled_from_pooled_store(self, monkeypatch) -> None:
        """_sample_db_pool_gauge populates nova_db_pool_* from a pooled store,
        and is a safe no-op when the store has no pool (ADR-0221)."""
        pytest.importorskip("prometheus_client")
        from novafabric.server import deps, observability

        metrics = observability.maybe_http_metrics("test")
        assert metrics is not None

        class _PooledStore:
            def pool_stats(self):
                return (2, 5)

        monkeypatch.setattr(deps, "get_metadata_store_dep", lambda: _PooledStore())
        observability._sample_db_pool_gauge(metrics)
        body, _ = metrics.render()
        text = body.decode() if isinstance(body, bytes) else body
        assert 'nova_db_pool_in_use{app="test",pool="metadata"} 2.0' in text
        assert 'nova_db_pool_size{app="test",pool="metadata"} 5.0' in text

        # No pool_stats → no-op, no exception.
        class _PlainStore:
            pass

        monkeypatch.setattr(deps, "get_metadata_store_dep", lambda: _PlainStore())
        observability._sample_db_pool_gauge(metrics)  # must not raise

    def test_metrics_operator_exemption(self, db_path: Path) -> None:
        pytest.importorskip("prometheus_client")
        cfg = ServerConfig(
            db_path=str(db_path),
            oidc=OidcConfig(issuer_url="https://issuer.example", audience="nova"),
            observability=ObservabilityConfig(metrics_exempt=True),
        )
        c = _client(cfg)
        resp = c.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")

    def test_metrics_route_label_is_template_never_raw_path(
        self, client: TestClient
    ) -> None:
        # Normative privacy/cardinality rule: raw path segments (IDs) must
        # never appear as label values — only the registered route template.
        pytest.importorskip("prometheus_client")
        raw_id = "raw-asset-id-5f2c9b7e0000"
        client.get(f"/v0/assets/{raw_id}")
        text = client.get("/metrics").text
        assert raw_id not in text
        assert 'route="/v0/assets/{asset_id}"' in text

    def test_metrics_unmatched_paths_collapse(self, client: TestClient) -> None:
        pytest.importorskip("prometheus_client")
        client.get("/no/such/path/tenant-4242")
        text = client.get("/metrics").text
        assert "tenant-4242" not in text
        assert 'route="unmatched"' in text


# --------------------------------------------------------------------------- #
# Second-slice metric inventory (ADR-0182 D4)
# --------------------------------------------------------------------------- #


class TestMetricInventory:
    def test_new_metric_families_present_in_exposition(
        self, client: TestClient
    ) -> None:
        pytest.importorskip("prometheus_client")
        text = client.get("/metrics").text
        assert "nova_ingest_events_total" in text
        assert "nova_readyz_check_status" in text
        # Pool gauges are registered (family discoverable) but sample-less:
        # neither backend keeps a connection pool today — honest absence.
        assert "nova_db_pool_in_use" in text
        assert "nova_db_pool_size" in text
        assert 'nova_db_pool_in_use{' not in text  # no fabricated samples
        assert 'nova_db_pool_size{' not in text

    def test_ingest_counter_labels_preinitialized_at_zero(
        self, client: TestClient
    ) -> None:
        pytest.importorskip("prometheus_client")
        text = client.get("/metrics").text
        for encoding in ("zip", "json"):
            for outcome in ("accepted", "rejected"):
                sample = (
                    f'nova_ingest_events_total{{app="server",'
                    f'encoding="{encoding}",outcome="{outcome}"}}'
                )
                assert sample in text, f"missing pre-initialised sample: {sample}"

    def test_ingest_counter_counts_rejected_capsule_upload(
        self, client: TestClient
    ) -> None:
        pytest.importorskip("prometheus_client")
        resp = client.post(
            "/v0/capsules",
            files={"capsule": ("x.zip", b"not a zip archive", "application/zip")},
        )
        assert resp.status_code == 400
        text = client.get("/metrics").text
        match = re.search(
            r'nova_ingest_events_total\{app="server",encoding="zip",'
            r'outcome="rejected"\}\s+(\d+(?:\.\d+)?)',
            text,
        )
        assert match is not None
        assert float(match.group(1)) >= 1

    def test_ingest_counter_counts_accepted_capsule_upload(
        self, client: TestClient
    ) -> None:
        # conftest points NOVAFABRIC_HOME at a per-test tmp dir, so the
        # upload lands in an hermetic capsule store.
        pytest.importorskip("prometheus_client")
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "capsule.yaml",
                "run_id: 01JOBSERV0182TESTCAPSULE01\nschema_version: 0.2.0\n",
            )
        resp = client.post(
            "/v0/capsules",
            files={"capsule": ("c.zip", buf.getvalue(), "application/zip")},
        )
        assert resp.status_code == 201, resp.text
        text = client.get("/metrics").text
        match = re.search(
            r'nova_ingest_events_total\{app="server",encoding="zip",'
            r'outcome="accepted"\}\s+(\d+(?:\.\d+)?)',
            text,
        )
        assert match is not None
        assert float(match.group(1)) >= 1

    def test_readyz_gauge_reports_passing_checks(self, client: TestClient) -> None:
        pytest.importorskip("prometheus_client")
        client.get("/readyz")
        text = client.get("/metrics").text
        assert 'nova_readyz_check_status{app="server",check="db"} 1.0' in text
        # skipped/unknown never fail readiness → they read as passing (1).
        assert (
            'nova_readyz_check_status{app="server",check="object_store"} 1.0'
            in text
        )
        assert (
            'nova_readyz_check_status{app="server",check="migrations"} 1.0'
            in text
        )

    def test_readyz_gauge_flips_on_broken_db(self, tmp_path: Path) -> None:
        pytest.importorskip("prometheus_client")
        broken = tmp_path / "no-such-dir" / "x.db"
        c = _client(ServerConfig(db_path=str(broken), insecure_no_auth=True))
        assert c.get("/readyz").status_code == 503
        text = c.get("/metrics").text
        assert 'nova_readyz_check_status{app="server",check="db"} 0.0' in text


# --------------------------------------------------------------------------- #
# /health alias
# --------------------------------------------------------------------------- #


class TestHealthAlias:
    def test_health_alias_unchanged(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["service"] == "nova-server"
        assert set(body) == {"ok", "service", "version", "backend"}
