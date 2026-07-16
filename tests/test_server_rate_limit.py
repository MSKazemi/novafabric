"""Tests for the ADR-0179 in-process token-bucket rate limiter (first slice).

Contract: design/spec/rate-limiting-quotas-v0.md
  - disabled by default — zero behavior change (no headers, no 429)
  - enabled: burst allowed, then 429 with the ADR-0017 envelope,
    Retry-After (delta-seconds, ceil) and all three X-RateLimit-* headers
  - keying: principal (bearer-token id) > tenant > client IP;
    two principals never share a bucket; classes never share buckets
  - exemptions: /health /livez /readyz /metrics never limited, no headers
  - class mapping: /v0/admin* -> admin, write verbs/OTLP -> ingest, GET -> read
  - monotonic refill; bounded (LRU) bucket map
  - sustained limiting emits exactly one audit record per key per window
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import (  # noqa: E402
    QuotaConfig,
    RateLimitClassConfig,
    RateLimitsConfig,
    ServerConfig,
)
from novafabric.server.rate_limit import (  # noqa: E402
    CLASS_ADMIN,
    CLASS_INGEST,
    CLASS_READ,
    KEY_CLIENT_IP,
    KEY_PRINCIPAL,
    KEY_TENANT,
    TokenBucketLimiter,
    classify_route,
    resolve_key_parts,
)

RL_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")


def _client(cfg: ServerConfig) -> TestClient:
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _config(db_path: Path, rate_limits: RateLimitsConfig | None = None) -> ServerConfig:
    return ServerConfig(
        db_path=str(db_path),
        insecure_no_auth=True,  # ADR-0184 opt-out: anonymous admin for tests
        rate_limits=rate_limits or RateLimitsConfig(),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "rl-test.db"


def _tiny(rate: float = 0.001, burst: int = 3) -> RateLimitClassConfig:
    """A class budget with effectively no refill during a fast test."""
    return RateLimitClassConfig(rate=rate, burst=burst)


# --------------------------------------------------------------------------- #
# Disabled = no-op (the default)
# --------------------------------------------------------------------------- #


class TestDisabledDefault:
    def test_default_config_is_disabled(self) -> None:
        assert RateLimitsConfig().enabled is False
        assert ServerConfig().rate_limits.enabled is False

    def test_no_headers_and_no_429_when_disabled(self, db_path: Path) -> None:
        client = _client(_config(db_path))
        for _ in range(30):
            resp = client.get("/v0/assets")
            assert resp.status_code == 200
            for header in RL_HEADERS:
                assert header not in resp.headers
            assert "Retry-After" not in resp.headers

    def test_no_middleware_installed_when_disabled(self, db_path: Path) -> None:
        app = create_app(_config(db_path))
        assert not hasattr(app.state, "rate_limiter")


# --------------------------------------------------------------------------- #
# Enabled: burst, 429 contract, headers
# --------------------------------------------------------------------------- #


class TestLimitContract:
    def test_burst_allowed_then_429_with_envelope_and_headers(self, db_path: Path) -> None:
        rl = RateLimitsConfig(enabled=True, read=_tiny(burst=3))
        client = _client(_config(db_path, rl))

        # Full burst succeeds; X-RateLimit-Remaining counts down.
        remaining = []
        for _ in range(3):
            resp = client.get("/v0/assets")
            assert resp.status_code == 200
            assert resp.headers["X-RateLimit-Limit"] == "3"
            remaining.append(int(resp.headers["X-RateLimit-Remaining"]))
        assert remaining == [2, 1, 0]

        # Request burst+1 is limited: envelope + all headers.
        resp = client.get("/v0/assets")
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["code"] == "rate_limited"
        assert body["error"]["details"] == {"limit_class": "read"}
        assert isinstance(body["error"]["message"], str)
        assert resp.headers["X-RateLimit-Limit"] == "3"
        assert resp.headers["X-RateLimit-Remaining"] == "0"
        assert int(resp.headers["X-RateLimit-Reset"]) >= 1
        assert "Retry-After" in resp.headers

    def test_retry_after_is_sane_delta_seconds(self, db_path: Path) -> None:
        # rate=0.5 tokens/s => an empty bucket needs ceil(1/0.5)=2s for a token.
        rl = RateLimitsConfig(
            enabled=True, read=RateLimitClassConfig(rate=0.5, burst=1)
        )
        client = _client(_config(db_path, rl))
        assert client.get("/v0/assets").status_code == 200
        resp = client.get("/v0/assets")
        assert resp.status_code == 429
        retry_after = int(resp.headers["Retry-After"])
        assert 1 <= retry_after <= 2

    def test_429_is_counted_by_the_metrics_middleware(self, db_path: Path) -> None:
        # ADR-0182 metrics middleware must be OUTSIDE the limiter: 429
        # rejections appear in nova_http_requests_total.
        pytest.importorskip("prometheus_client")
        rl = RateLimitsConfig(enabled=True, read=_tiny(burst=1))
        client = _client(_config(db_path, rl))
        assert client.get("/v0/assets").status_code == 200
        assert client.get("/v0/assets").status_code == 429
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert 'status="429"' in metrics.text


# --------------------------------------------------------------------------- #
# Keying
# --------------------------------------------------------------------------- #


class TestKeying:
    def test_two_principals_do_not_share_a_bucket(self, db_path: Path) -> None:
        rl = RateLimitsConfig(enabled=True, read=_tiny(burst=2))
        client = _client(_config(db_path, rl))
        alice = {"Authorization": "Bearer token-alice"}
        bob = {"Authorization": "Bearer token-bob"}

        # Alice exhausts her bucket …
        for _ in range(2):
            assert client.get("/v0/assets", headers=alice).status_code == 200
        assert client.get("/v0/assets", headers=alice).status_code == 429
        # … Bob is unaffected.
        assert client.get("/v0/assets", headers=bob).status_code == 200

    def test_same_principal_shares_a_bucket(self, db_path: Path) -> None:
        rl = RateLimitsConfig(enabled=True, read=_tiny(burst=1))
        client = _client(_config(db_path, rl))
        hdrs = {"Authorization": "Bearer same-token"}
        assert client.get("/v0/assets", headers=hdrs).status_code == 200
        assert client.get("/v0/assets", headers=hdrs).status_code == 429

    def test_resolve_key_precedence(self) -> None:
        # principal beats tenant beats client IP
        kt, kv = resolve_key_parts("Bearer tok", "tenant-1", "10.0.0.1")
        assert kt == KEY_PRINCIPAL
        assert "tok" not in kv  # digest, never the raw token
        assert resolve_key_parts(None, "tenant-1", "10.0.0.1") == (KEY_TENANT, "tenant-1")
        assert resolve_key_parts(None, None, "10.0.0.1") == (KEY_CLIENT_IP, "10.0.0.1")
        assert resolve_key_parts("", None, None) == (KEY_CLIENT_IP, "unknown")

    def test_classes_do_not_share_buckets(self) -> None:
        rl = RateLimitsConfig(enabled=True, read=_tiny(burst=1), ingest=_tiny(burst=1))
        limiter = TokenBucketLimiter(rl)
        assert limiter.check("principal", "k", CLASS_READ).allowed
        assert not limiter.check("principal", "k", CLASS_READ).allowed
        # Same key, other class: fresh bucket.
        assert limiter.check("principal", "k", CLASS_INGEST).allowed


# --------------------------------------------------------------------------- #
# Route classification + exemptions
# --------------------------------------------------------------------------- #


class TestClassificationAndExemptions:
    @pytest.mark.parametrize("path", ["/health", "/livez", "/readyz", "/metrics"])
    def test_exempt_paths_classify_to_none(self, path: str) -> None:
        assert classify_route("GET", path) is None

    def test_classify_admin_read_ingest(self) -> None:
        assert classify_route("GET", "/v0/admin/roles") == CLASS_ADMIN
        assert classify_route("POST", "/v0/admin/roles") == CLASS_ADMIN
        assert classify_route("GET", "/v0/roles") == CLASS_ADMIN
        assert classify_route("GET", "/v0/assets") == CLASS_READ
        assert classify_route("POST", "/v0/assets") == CLASS_INGEST
        assert classify_route("PUT", "/v0/assets/a/promote") == CLASS_INGEST
        assert classify_route("DELETE", "/v0/assets/a") == CLASS_INGEST
        # OTLP ingest paths are ingest-class regardless of verb.
        assert classify_route("POST", "/v0/otlp/v1/traces") == CLASS_INGEST

    def test_exempt_endpoints_never_429_under_saturation(self, db_path: Path) -> None:
        rl = RateLimitsConfig(
            enabled=True,
            read=_tiny(burst=1),
            ingest=_tiny(burst=1),
            admin=_tiny(burst=1),
        )
        client = _client(_config(db_path, rl))
        # Saturate the caller's read bucket.
        client.get("/v0/assets")
        assert client.get("/v0/assets").status_code == 429
        for path in ("/health", "/livez", "/readyz", "/metrics"):
            for _ in range(5):
                resp = client.get(path)
                assert resp.status_code != 429, path
                for header in RL_HEADERS:
                    assert header not in resp.headers, (path, header)

    def test_admin_route_uses_admin_class(self, db_path: Path) -> None:
        rl = RateLimitsConfig(
            enabled=True,
            admin=_tiny(burst=1),
            read=RateLimitClassConfig(rate=50, burst=100),
        )
        client = _client(_config(db_path, rl))
        assert client.get("/v0/admin/roles").status_code == 200
        resp = client.get("/v0/admin/roles")
        assert resp.status_code == 429
        assert resp.json()["error"]["details"]["limit_class"] == "admin"
        # The read class is untouched by admin saturation.
        assert client.get("/v0/assets").status_code == 200


# --------------------------------------------------------------------------- #
# Refill mechanics (injected monotonic clock) + bounded map
# --------------------------------------------------------------------------- #


class TestRefill:
    def test_monotonic_refill(self) -> None:
        now = [0.0]
        rl = RateLimitsConfig(
            enabled=True, read=RateLimitClassConfig(rate=1, burst=2)
        )
        limiter = TokenBucketLimiter(rl, clock=lambda: now[0])

        assert limiter.check("principal", "k", CLASS_READ).allowed
        assert limiter.check("principal", "k", CLASS_READ).allowed
        denied = limiter.check("principal", "k", CLASS_READ)
        assert not denied.allowed
        assert denied.retry_after == 1  # ceil(1 token / 1 per s)

        now[0] += 1.0  # one token refilled
        assert limiter.check("principal", "k", CLASS_READ).allowed
        assert not limiter.check("principal", "k", CLASS_READ).allowed

        now[0] += 10.0  # refill clamps at burst
        d1 = limiter.check("principal", "k", CLASS_READ)
        assert d1.allowed
        assert d1.remaining == 1
        assert limiter.check("principal", "k", CLASS_READ).allowed
        assert not limiter.check("principal", "k", CLASS_READ).allowed

    def test_bucket_map_is_lru_bounded(self) -> None:
        rl = RateLimitsConfig(enabled=True)
        limiter = TokenBucketLimiter(rl, max_buckets=10)
        for i in range(1000):
            limiter.check("principal", f"key-{i}", CLASS_READ)
        assert len(limiter) <= 10


# --------------------------------------------------------------------------- #
# Sustained-limiting audit
# --------------------------------------------------------------------------- #


class TestSustainedLimitingAudit:
    def _audit_records(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text().splitlines()
            if line.strip()
        ]

    def test_audit_emitted_once_per_key_per_window(
        self, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit_file = tmp_path / "audit.jsonl"
        monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(audit_file))
        rl = RateLimitsConfig(
            enabled=True,
            read=_tiny(burst=1),
            audit_threshold_rejections=3,
            audit_window_seconds=60,
        )
        client = _client(_config(db_path, rl))
        client.get("/v0/assets")  # consume the single token
        for _ in range(10):  # 10 rejections, threshold 3, one window
            assert client.get("/v0/assets").status_code == 429

        records = [
            r
            for r in self._audit_records(audit_file)
            if r.get("action") == "rate_limit_sustained"
        ]
        assert len(records) == 1
        args = records[0]["args"]
        assert args["event"] == "rate_limit_sustained"
        assert args["key_type"] == "client_ip"
        assert args["key_hash"].startswith("sha256:")
        assert "testclient" not in json.dumps(args)  # raw key never stored
        assert args["limit_class"] == "read"
        assert args["rejected_count"] == 3
        assert args["window_start"]
        assert args["emitted_at"]

    def test_audit_warning_logged(
        self, db_path: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv(
            "NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl")
        )
        rl = RateLimitsConfig(
            enabled=True, read=_tiny(burst=1), audit_threshold_rejections=2
        )
        client = _client(_config(db_path, rl))
        client.get("/v0/assets")
        with caplog.at_level("WARNING", logger="novafabric.server.rate_limit"):
            for _ in range(3):
                client.get("/v0/assets")
        assert any("sustained rate limiting" in r.message for r in caplog.records)

    def test_new_window_can_audit_again(self) -> None:
        now = [0.0]
        emitted: list[dict] = []
        rl = RateLimitsConfig(
            enabled=True,
            read=_tiny(rate=0.0001, burst=1),
            audit_threshold_rejections=2,
            audit_window_seconds=10,
        )
        limiter = TokenBucketLimiter(
            rl, clock=lambda: now[0], audit_hook=emitted.append
        )
        limiter.check("principal", "k", CLASS_READ)  # consume
        for _ in range(5):
            limiter.check("principal", "k", CLASS_READ)
        assert len(emitted) == 1
        now[0] += 11.0  # window rolls
        for _ in range(5):
            limiter.check("principal", "k", CLASS_READ)
        assert len(emitted) == 2


# --------------------------------------------------------------------------- #
# Config: env overrides + quota validation
# --------------------------------------------------------------------------- #


class TestConfig:
    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_RATE_LIMITS_ENABLED", "true")
        monkeypatch.setenv("NOVAFABRIC_SERVER_RATE_LIMITS_INGEST_RATE", "7.5")
        monkeypatch.setenv("NOVAFABRIC_SERVER_RATE_LIMITS_INGEST_BURST", "15")
        monkeypatch.setenv(
            "NOVAFABRIC_SERVER_RATE_LIMITS_AUDIT_THRESHOLD_REJECTIONS", "5"
        )
        cfg = ServerConfig()
        assert cfg.rate_limits.enabled is True
        assert cfg.rate_limits.ingest.rate == 7.5
        assert cfg.rate_limits.ingest.burst == 15
        assert cfg.rate_limits.audit_threshold_rejections == 5
        # Untouched classes keep the spec defaults.
        assert (cfg.rate_limits.read.rate, cfg.rate_limits.read.burst) == (50, 100)
        assert (cfg.rate_limits.admin.rate, cfg.rate_limits.admin.burst) == (10, 20)

    def test_spec_defaults(self) -> None:
        rl = RateLimitsConfig()
        assert (rl.ingest.rate, rl.ingest.burst) == (100, 200)
        assert (rl.read.rate, rl.read.burst) == (50, 100)
        assert (rl.admin.rate, rl.admin.burst) == (10, 20)
        assert rl.audit_threshold_rejections == 100
        assert rl.audit_window_seconds == 60
        assert rl.quota is None

    def test_quota_hard_must_be_gte_soft(self) -> None:
        with pytest.raises(ValueError):
            QuotaConfig(max_bytes_soft=100, max_bytes_hard=50)
        q = QuotaConfig(max_bytes_soft=100, max_bytes_hard=200)
        assert q.max_bytes_hard == 200
        # 0 = unlimited, always valid
        assert QuotaConfig().max_capsules_soft == 0
