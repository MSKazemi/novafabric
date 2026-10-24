"""``NOVAFABRIC_SERVER_*`` overrides obey the same bounds as ``server.yaml`` (review server-14).

The overrides were plain attribute assignments after validation, so values the
YAML path rejects were accepted from the environment: ``SPOOL_CHUNK_BYTES=0``
made every upload "Capsule file is empty", ``-1`` turned the chunked read into
an unbounded one, ``MAX_CONCURRENT_UNPACK=0`` hung every upload on
``Semaphore(0)``, and ``RATE_LIMITS_READ_RATE=0`` divided by zero.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from novafabric.server.config import ServerConfig


@pytest.mark.parametrize(
    ("env", "value", "field"),
    [
        ("NOVAFABRIC_SERVER_INGEST_SPOOL_CHUNK_BYTES", "0", "ingest.spool_chunk_bytes"),
        ("NOVAFABRIC_SERVER_INGEST_SPOOL_CHUNK_BYTES", "-1", "ingest.spool_chunk_bytes"),
        ("NOVAFABRIC_SERVER_INGEST_MAX_CONCURRENT_UNPACK", "0", "ingest.max_concurrent_unpack"),
        ("NOVAFABRIC_SERVER_RATE_LIMITS_READ_RATE", "0", "rate_limits.read.rate"),
        ("NOVAFABRIC_SERVER_RATE_LIMITS_ADMIN_BURST", "0", "rate_limits.admin.burst"),
        ("NOVAFABRIC_SERVER_WEBHOOKS_MAX_ATTEMPTS", "1000", "webhooks.max_attempts"),
        ("NOVAFABRIC_SERVER_STEP_UP_MAX_AGE_SECONDS", "0", "step_up.max_age_seconds"),
        ("NOVAFABRIC_SERVER_USAGE_FLUSH_INTERVAL_S", "0", "usage.flush_interval_s"),
        ("NOVAFABRIC_SERVER_PORT", "99999", "port"),
    ],
)
def test_an_out_of_range_override_fails_at_load(
    monkeypatch: pytest.MonkeyPatch, env: str, value: str, field: str
) -> None:
    monkeypatch.setenv(env, value)
    with pytest.raises(ValidationError) as excinfo:
        ServerConfig()
    assert field in str(excinfo.value)


def test_in_range_overrides_still_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_SERVER_INGEST_SPOOL_CHUNK_BYTES", "65536")
    monkeypatch.setenv("NOVAFABRIC_SERVER_RATE_LIMITS_READ_RATE", "2.5")
    monkeypatch.setenv("NOVAFABRIC_SERVER_PORT", "8080")
    cfg = ServerConfig()
    assert cfg.ingest.spool_chunk_bytes == 65536
    assert cfg.rate_limits.read.rate == 2.5
    assert cfg.port == 8080


def test_validation_keeps_the_secrets_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVAFABRIC_SERVER_TOKEN", "tok")
    monkeypatch.setenv("NOVAFABRIC_SCIM_TOKEN", "scim")
    cfg = ServerConfig()
    assert (cfg.local_token, cfg.scim_token) == ("tok", "scim")
