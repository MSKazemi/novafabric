"""ADR-0184 D3: insecure (anonymous-admin) server starts write an audit entry.

- an insecure start appends exactly one ``server.insecure_no_auth`` entry to the
  hash-chained audit log, and the chain still verifies;
- a secure start (local token) and an insecure flag shadowed by OIDC write none;
- a refused start (non-loopback bind without confirmation) writes none;
- an unwritable audit log refuses the insecure start (fail closed).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.audit import AuditEventType, AuditLog  # noqa: E402
from novafabric.audit.siem_export import OCSF_CLASS_MAP  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import OidcConfig, ServerConfig  # noqa: E402
from novafabric.server.insecure_audit import (  # noqa: E402
    AUDIT_LOG_PATH_ENV,
    INSECURE_START_ACTOR,
    InsecureModeAuditError,
    insecure_mode_active,
    record_insecure_start,
)


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(path))
    return path


def _entries(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _start(tmp_path: Path, **cfg: object) -> None:
    config = ServerConfig(db_path=str(tmp_path / "test.db"), **cfg)  # type: ignore[arg-type]
    with TestClient(create_app(config), raise_server_exceptions=True) as client:
        client.get("/health")


def test_insecure_start_writes_one_audit_entry(tmp_path: Path, audit_path: Path) -> None:
    _start(tmp_path, insecure_no_auth=True)
    entries = _entries(audit_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["event_type"] == AuditEventType.SERVER_INSECURE_NO_AUTH.value
    assert entry["actor"] == INSECURE_START_ACTOR
    assert entry["resource_id"] == "nova-server:127.0.0.1:7433"
    details = entry["details"]
    assert isinstance(details, dict)
    assert details["loopback"] is True
    assert details["i_know_this_is_public"] is False
    assert details["adr"] == "ADR-0184"
    assert AuditLog(audit_path).verify() == []


def test_each_insecure_start_is_chained(tmp_path: Path, audit_path: Path) -> None:
    _start(tmp_path, insecure_no_auth=True)
    _start(tmp_path, insecure_no_auth=True)
    entries = _entries(audit_path)
    assert len(entries) == 2
    assert entries[1]["prev_hash"] == entries[0]["entry_hash"]
    assert AuditLog(audit_path).verify() == []


def test_secure_start_writes_nothing(tmp_path: Path, audit_path: Path) -> None:
    _start(tmp_path, local_token="tok")
    assert _entries(audit_path) == []


def test_oidc_shadows_insecure_flag(audit_path: Path) -> None:
    cfg = ServerConfig(
        insecure_no_auth=True,
        oidc=OidcConfig(issuer_url="https://idp.example", audience="nova"),
    )
    assert insecure_mode_active(cfg) is False
    assert record_insecure_start(cfg) is None
    assert _entries(audit_path) == []


def test_public_bind_confirmation_is_recorded(audit_path: Path) -> None:
    cfg = ServerConfig(
        host="0.0.0.0",  # noqa: S104 — the point of the test
        insecure_no_auth=True,
        i_know_this_is_public=True,
    )
    entry = record_insecure_start(cfg)
    assert entry is not None
    assert entry.details["loopback"] is False
    assert entry.details["i_know_this_is_public"] is True


def test_refused_public_bind_writes_nothing(tmp_path: Path, audit_path: Path) -> None:
    with pytest.raises(ValueError, match="ADR-0184"):
        ServerConfig(
            db_path=str(tmp_path / "test.db"),
            host="0.0.0.0",  # noqa: S104
            insecure_no_auth=True,
        )
    assert _entries(audit_path) == []


def test_unwritable_audit_log_refuses_insecure_start(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file where a directory is expected")
    cfg = ServerConfig(insecure_no_auth=True)
    with pytest.raises(InsecureModeAuditError, match="Refusing to start"):
        record_insecure_start(cfg, audit_log_path=blocker / "audit.jsonl")


def test_unwritable_audit_log_fails_app_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv(AUDIT_LOG_PATH_ENV, str(blocker / "audit.jsonl"))
    with pytest.raises(InsecureModeAuditError):
        _start(tmp_path, insecure_no_auth=True)


def test_event_type_is_mapped_for_siem_export() -> None:
    assert AuditEventType.SERVER_INSECURE_NO_AUTH.value in OCSF_CLASS_MAP
