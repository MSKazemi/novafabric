"""Tests for ADR-0179 storage-quota enforcement (second slice).

Contract: design/spec/rate-limiting-quotas-v0.md §Quota accounting
  - usage derived from the existing capsule store (count + total bytes)
  - checked at ingest only, warn-then-reject:
      soft ⇒ write succeeds + X-NovaFabric-Quota-Warning + one audit/window
      hard ⇒ 429 quota_exceeded envelope (usage/limit in details),
             Retry-After omitted (quota does not decay on a clock)
  - 0 = unlimited, short-circuits without querying the store
  - derivation query TTL-cached (bounded staleness on hot ingest paths)
  - feature inert unless rate_limits.enabled AND a non-zero limit exists
"""

from __future__ import annotations

import io
import json
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
)
from novafabric.server.quotas import (  # noqa: E402
    QUOTA_WARNING_HEADER,
    QuotaChecker,
    QuotaDecision,
    QuotaUsage,
    QuotaViolation,
    measure_capsule_store,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _client(cfg: ServerConfig) -> TestClient:
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _config(db_path: Path, rate_limits: RateLimitsConfig | None = None) -> ServerConfig:
    return ServerConfig(
        db_path=str(db_path),
        insecure_no_auth=True,  # ADR-0184 opt-out: anonymous admin for tests
        rate_limits=rate_limits or RateLimitsConfig(),
    )


def _quota_rl(quota: QuotaConfig | None, **kwargs: int) -> RateLimitsConfig:
    return RateLimitsConfig(enabled=True, quota=quota, **kwargs)


def _capsule_zip(run_id: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("capsule.yaml", f"run_id: {run_id}\nstatus: completed\n")
    return buf.getvalue()


def _upload(client: TestClient, run_id: str) -> httpx.Response:
    return client.post(
        "/v0/capsules",
        files={"capsule": (f"{run_id}.zip", _capsule_zip(run_id), "application/zip")},
    )


def _seed_capsule(capsule_dir: Path, run_id: str, extra_bytes: int = 0) -> None:
    dest = capsule_dir / run_id
    dest.mkdir(parents=True)
    (dest / "capsule.yaml").write_text(f"run_id: {run_id}\nstatus: completed\n")
    if extra_bytes:
        (dest / "blob.bin").write_bytes(b"x" * extra_bytes)


def _audit_records(path: Path, action: str) -> list[dict]:
    if not path.exists():
        return []
    return [
        r
        for r in (json.loads(line) for line in path.read_text().splitlines() if line.strip())
        if r.get("action") == action
    ]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "quota-test.db"


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


# --------------------------------------------------------------------------- #
# Inert configurations (zero behavior change)
# --------------------------------------------------------------------------- #


class TestInert:
    def test_absent_quota_block_installs_nothing(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        client = _client(_config(db_path, _quota_rl(None)))
        assert getattr(client.app.state, "quota_checker", None) is None
        resp = _upload(client, "r-absent")
        assert resp.status_code == 201
        assert QUOTA_WARNING_HEADER not in resp.headers

    def test_all_zero_limits_installs_nothing(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        client = _client(_config(db_path, _quota_rl(QuotaConfig())))
        assert getattr(client.app.state, "quota_checker", None) is None
        resp = _upload(client, "r-zeros")
        assert resp.status_code == 201
        assert QUOTA_WARNING_HEADER not in resp.headers

    def test_rate_limits_disabled_gates_quota_too(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        # Spec: enabled: false ⇒ limiter AND quotas fully inert.
        rl = RateLimitsConfig(enabled=False, quota=QuotaConfig(max_capsules_hard=1))
        _seed_capsule(capsule_dir, "seed-0")
        client = _client(_config(db_path, rl))
        assert getattr(client.app.state, "quota_checker", None) is None
        assert _upload(client, "r-gated").status_code == 201

    def test_zero_limits_short_circuit_without_querying(self) -> None:
        calls: list[int] = []

        def provider() -> QuotaUsage:
            calls.append(1)
            return QuotaUsage(capsules=0, total_bytes=0)

        checker = QuotaChecker(QuotaConfig(), provider)
        assert checker.enabled is False
        assert checker.check().outcome == "ok"
        assert calls == []

        none_checker = QuotaChecker(None, provider)
        assert none_checker.check().outcome == "ok"
        assert calls == []


# --------------------------------------------------------------------------- #
# Soft limit: warn (write succeeds, header + one audit per window)
# --------------------------------------------------------------------------- #


class TestSoftWarn:
    def test_write_succeeds_with_warning_header_and_single_audit(
        self, db_path: Path, capsule_dir: Path, audit_file: Path
    ) -> None:
        _seed_capsule(capsule_dir, "seed-0")
        quota = QuotaConfig(max_capsules_soft=1)
        client = _client(_config(db_path, _quota_rl(quota)))

        resp = _upload(client, "r-warn-1")
        assert resp.status_code == 201  # soft ⇒ the write still succeeds
        assert resp.headers[QUOTA_WARNING_HEADER] == "capsules 1/1"
        assert (capsule_dir / "r-warn-1" / "capsule.yaml").exists()

        # More writes in the same audit window: still warned, audited once.
        resp2 = _upload(client, "r-warn-2")
        assert resp2.status_code == 201
        assert QUOTA_WARNING_HEADER in resp2.headers
        records = _audit_records(audit_file, "quota_soft_exceeded")
        assert len(records) == 1
        args = records[0]["args"]
        assert args["event"] == "quota_soft_exceeded"
        assert args["kind"] == "capsules"
        assert args["usage"] == 1
        assert args["limit"] == 1
        assert args["window_start"]
        assert args["emitted_at"]

    def test_bytes_soft_limit_warns(
        self, db_path: Path, capsule_dir: Path, audit_file: Path
    ) -> None:
        _seed_capsule(capsule_dir, "seed-big", extra_bytes=500)
        quota = QuotaConfig(max_bytes_soft=100)
        client = _client(_config(db_path, _quota_rl(quota)))
        resp = _upload(client, "r-bytes-warn")
        assert resp.status_code == 201
        header = resp.headers[QUOTA_WARNING_HEADER]
        assert header.startswith("bytes ")
        assert header.endswith("/100")
        assert _audit_records(audit_file, "quota_soft_exceeded")


# --------------------------------------------------------------------------- #
# Hard limit: reject (429 quota_exceeded, no Retry-After, store untouched)
# --------------------------------------------------------------------------- #


class TestHardReject:
    def test_429_envelope_with_usage_and_limit(
        self, db_path: Path, capsule_dir: Path, audit_file: Path
    ) -> None:
        for i in range(2):
            _seed_capsule(capsule_dir, f"seed-{i}")
        quota = QuotaConfig(max_capsules_soft=1, max_capsules_hard=2)
        client = _client(_config(db_path, _quota_rl(quota)))

        resp = _upload(client, "r-reject")
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["code"] == "quota_exceeded"
        assert body["error"]["details"] == {
            "kind": "capsules",
            "usage": 2,
            "limit": 2,
        }
        # Quota does not decay on a clock — Retry-After MUST be omitted.
        assert "Retry-After" not in resp.headers
        # The rejected write left the store untouched.
        assert not (capsule_dir / "r-reject").exists()

        # Repeated rejections in one window ⇒ exactly one audit record.
        assert _upload(client, "r-reject-2").status_code == 429
        records = _audit_records(audit_file, "quota_hard_exceeded")
        assert len(records) == 1
        assert records[0]["args"]["kind"] == "capsules"

    def test_bytes_hard_limit_rejects(
        self, db_path: Path, capsule_dir: Path, audit_file: Path
    ) -> None:
        _seed_capsule(capsule_dir, "seed-big", extra_bytes=1000)
        quota = QuotaConfig(max_bytes_hard=100)
        client = _client(_config(db_path, _quota_rl(quota)))
        resp = _upload(client, "r-bytes-reject")
        assert resp.status_code == 429
        details = resp.json()["error"]["details"]
        assert details["kind"] == "bytes"
        assert details["limit"] == 100
        assert details["usage"] >= 1000

    def test_scores_route_is_guarded_too(
        self, db_path: Path, capsule_dir: Path, audit_file: Path
    ) -> None:
        _seed_capsule(capsule_dir, "seed-0")
        quota = QuotaConfig(max_capsules_hard=1)
        client = _client(_config(db_path, _quota_rl(quota)))
        resp = client.post("/v0/capsules/seed-0/scores", json={})
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "quota_exceeded"


# --------------------------------------------------------------------------- #
# TTL cache (bounded staleness of the derived counts)
# --------------------------------------------------------------------------- #


class TestCountCache:
    def test_provider_called_once_within_ttl(self) -> None:
        now = [0.0]
        calls: list[int] = []

        def provider() -> QuotaUsage:
            calls.append(1)
            return QuotaUsage(capsules=0, total_bytes=0)

        checker = QuotaChecker(
            QuotaConfig(max_capsules_soft=100),
            provider,
            clock=lambda: now[0],
            cache_ttl=5.0,
        )
        assert checker.check().outcome == "ok"
        assert checker.check().outcome == "ok"
        assert checker.check().outcome == "ok"
        assert len(calls) == 1  # cached within the TTL

        now[0] += 5.1  # TTL elapsed ⇒ re-count
        assert checker.check().outcome == "ok"
        assert len(calls) == 2

    def test_invalidate_forces_recount(self) -> None:
        calls: list[int] = []

        def provider() -> QuotaUsage:
            calls.append(1)
            return QuotaUsage(capsules=0, total_bytes=0)

        checker = QuotaChecker(QuotaConfig(max_capsules_soft=10), provider)
        checker.check()
        checker.invalidate()
        checker.check()
        assert len(calls) == 2


# --------------------------------------------------------------------------- #
# Checker unit semantics
# --------------------------------------------------------------------------- #


class TestCheckerSemantics:
    def _checker(self, quota: QuotaConfig, usage: QuotaUsage) -> QuotaChecker:
        return QuotaChecker(quota, lambda: usage, audit_hook=lambda _p: None)

    def test_reject_wins_over_warn_across_kinds(self) -> None:
        quota = QuotaConfig(max_capsules_soft=1, max_bytes_hard=10)
        decision = self._checker(quota, QuotaUsage(capsules=5, total_bytes=50)).check()
        assert decision.outcome == "reject"
        hard = decision.hard
        assert hard is not None
        assert (hard.kind, hard.usage, hard.limit) == ("bytes", 50, 10)

    def test_below_limits_is_ok(self) -> None:
        quota = QuotaConfig(
            max_capsules_soft=10, max_capsules_hard=20, max_bytes_soft=100
        )
        decision = self._checker(quota, QuotaUsage(capsules=9, total_bytes=99)).check()
        assert decision.outcome == "ok"
        assert decision.violations == ()

    def test_warning_header_lists_all_soft_violations(self) -> None:
        decision = QuotaDecision(
            outcome="warn",
            violations=(
                QuotaViolation(kind="capsules", usage=5, limit=5, severity="soft"),
                QuotaViolation(kind="bytes", usage=200, limit=100, severity="soft"),
            ),
        )
        assert decision.warning_header == "capsules 5/5, bytes 200/100"

    def test_audit_once_per_window_then_again_after_rollover(self) -> None:
        now = [0.0]
        emitted: list[dict] = []
        checker = QuotaChecker(
            QuotaConfig(max_capsules_soft=1),
            lambda: QuotaUsage(capsules=1, total_bytes=0),
            clock=lambda: now[0],
            cache_ttl=0.0,  # never cache — every check re-counts
            audit_window_seconds=10.0,
            audit_hook=emitted.append,
        )
        for _ in range(5):
            assert checker.check().outcome == "warn"
        assert len(emitted) == 1
        now[0] += 11.0  # window rolls
        assert checker.check().outcome == "warn"
        assert len(emitted) == 2
        assert all(p["event"] == "quota_soft_exceeded" for p in emitted)


# --------------------------------------------------------------------------- #
# Store measurement
# --------------------------------------------------------------------------- #


class TestMeasureCapsuleStore:
    def test_counts_capsules_and_bytes(self, tmp_path: Path) -> None:
        cdir = tmp_path / "capsules"
        cdir.mkdir()
        _seed_capsule(cdir, "a", extra_bytes=10)
        _seed_capsule(cdir, "b")
        (cdir / "not-a-capsule").mkdir()  # no capsule.yaml ⇒ not counted
        (cdir / "stray.txt").write_bytes(b"12345")  # bytes still counted
        usage = measure_capsule_store(cdir)
        assert usage.capsules == 2
        expected_bytes = sum(
            f.stat().st_size for f in cdir.rglob("*") if f.is_file()
        )
        assert usage.total_bytes == expected_bytes
        assert usage.total_bytes >= 15

    def test_missing_dir_is_zero(self, tmp_path: Path) -> None:
        assert measure_capsule_store(tmp_path / "nope") == QuotaUsage(
            capsules=0, total_bytes=0
        )
