"""Support-bundle redaction reads values, not only key names (review storage-backup-14).

``redact_value`` blanked a subtree only when its *key* named a secret, and the v3
credential value patterns ran only on log lines. A URL's userinfo was no pattern at
all. So ``database_url: postgresql://nova:hunter2@db/nova``, an OTLP endpoint with a
password, or a bare token under a neutral key shipped verbatim in
``config.redacted.yaml``, against ADR-0187 D2's "no credentials".
"""

from __future__ import annotations

import pytest

from novafabric.support_bundle._redact import (
    REDACTED,
    REDACTION_RULESET_VERSION,
    redact_line,
    redact_value,
)

CONFIG = {
    "self_tracing_endpoint": "https://otel:Sup3rS3cret@collector.internal:4318/v1/traces",
    "database_url": "postgresql://nova:hunter2@db/nova",
    "notes": ["provider said ghp_" + "A" * 36],
    "nested": {"upstream": "postgres://u:p@ss@word@db:5432/x"},
    "capsule_hash": "sha256:" + "ab" * 32,  # integrity values are preserved
    "run_id": "01HXAY7M5JZ8R7K4P9DPBYK2WX",
}


def test_credentials_under_ordinary_keys_are_redacted() -> None:
    out = redact_value(CONFIG)
    flat = repr(out)
    for secret in ("Sup3rS3cret", "hunter2", "ghp_", "ss@word", "p@ss"):
        assert secret not in flat
    assert out["database_url"] == f"postgresql://nova:{REDACTED}@db/nova"
    assert out["self_tracing_endpoint"].endswith("@collector.internal:4318/v1/traces")
    assert out["capsule_hash"] == CONFIG["capsule_hash"]
    assert out["run_id"] == CONFIG["run_id"]


@pytest.mark.parametrize("line", [
    "connecting to postgresql://nova:hunter2@db/nova failed",
    "export to https://otel:Sup3rS3cret@collector:4318 refused",
])
def test_log_lines_lose_url_passwords(line: str) -> None:
    assert "hunter2" not in redact_line(line) and "Sup3rS3cret" not in redact_line(line)


def test_a_url_without_a_password_is_unchanged() -> None:
    assert redact_line("https://user@host/path") == "https://user@host/path"
    assert redact_value({"u": "https://host:443/x"}) == {"u": "https://host:443/x"}


def test_the_ruleset_version_says_so() -> None:
    assert REDACTION_RULESET_VERSION == "v4"
