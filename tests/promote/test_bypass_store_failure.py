"""A bypass whose file cannot be stored is withdrawn from the audit log (review r8, hole 2).

``put_bypass`` writes the ``seal.bypass.create`` entry *before* renaming the staged file
into place (fail closed on the audit). If the rename then failed, the log recorded a
bypass that did not exist, the staged file was removed, and ``nova serve`` answered a
raw 500 without its dashboard record. Now a failed rename appends a compensating
``seal.bypass.abort`` entry, removes the staging file and raises
:class:`BypassStoreError`; the route records the attempt and answers 503.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from novafabric.audit import AuditLog, resolve_audit_log_path
from novafabric.audit import _log as audit_log_module
from novafabric.promote import bundle_store as bundle_store_mod
from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.bypass_notify import BypassEvent, NullBypassNotifier
from novafabric.promote.exceptions import BypassRefusedError, BypassStoreError

ENVELOPE = b'{"payloadType":"x","payload":"","signatures":[]}'
UNTIL = "2099-01-01T00:00:00+00:00"
REASON = "Emergency bypass for incident INC-8080: the checker is unreachable and prod is down."


def _entries() -> list[dict[str, object]]:
    path = resolve_audit_log_path()
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _fail_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bundle_store_mod.os, "replace", boom)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[BypassEvent] = []

    def notify(self, event: BypassEvent) -> None:
        self.events.append(event)


def test_a_failed_rename_appends_an_abort_entry_and_raises_a_named_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _Recorder()
    store = PromoteBundleStore(tmp_path / "data", notifier=sent)
    _fail_rename(monkeypatch)

    with pytest.raises(BypassStoreError) as info:
        store.put_bypass("cap-r8", ENVELOPE, UNTIL, actor="tester")

    assert isinstance(info.value, BypassRefusedError)
    rows = _entries()
    assert [r["event_type"] for r in rows] == ["seal.bypass.create", "seal.bypass.abort"]
    create, abort = rows
    assert isinstance(create["details"], dict) and isinstance(abort["details"], dict)
    assert abort["details"]["bypass_uuid"] == create["details"]["bypass_uuid"]
    assert abort["resource_id"] == "cap-r8"
    assert abort["actor"] == "tester"
    assert abort["details"]["reason"] == "store_failed"
    assert AuditLog(resolve_audit_log_path()).verify() == []
    # Nothing is stored, nothing is left behind, nothing is announced.
    assert store.list_bypasses("cap-r8") == []
    assert list((tmp_path / "data" / "promote" / "cap-r8" / "bypass").iterdir()) == []
    assert sent.events == []


def test_a_failed_abort_entry_still_raises_the_named_error_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PromoteBundleStore(tmp_path / "data", notifier=NullBypassNotifier())
    _fail_rename(monkeypatch)
    real_append = audit_log_module.AuditLog.append

    def append_but_not_abort(self: AuditLog, event_type: object, *a: object, **kw: object):  # type: ignore[no-untyped-def]
        if str(getattr(event_type, "value", event_type)) == "seal.bypass.abort":
            raise OSError(5, "I/O error")
        return real_append(self, event_type, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(audit_log_module.AuditLog, "append", append_but_not_abort)

    with pytest.raises(BypassStoreError, match="abort entry could not be written"):
        store.put_bypass("cap-r8", ENVELOPE, UNTIL)
    assert store.list_bypasses("cap-r8") == []


def test_a_failed_staging_write_raises_the_named_error_and_audits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PromoteBundleStore(tmp_path / "data", notifier=NullBypassNotifier())
    real_write_text = Path.write_text

    def fail_staged(self: Path, *a: object, **kw: object) -> int:
        if self.name.endswith(".staged"):
            raise OSError(28, "No space left on device")
        return real_write_text(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", fail_staged)

    with pytest.raises(BypassStoreError):
        store.put_bypass("cap-r8", ENVELOPE, UNTIL)
    assert _entries() == []
    assert store.list_bypasses("cap-r8") == []


# ---------------------------------------------------------------------------
# nova serve
# ---------------------------------------------------------------------------


@pytest.fixture()
def serve_client(tmp_path: Path) -> Iterator[tuple[object, str]]:
    pytest.importorskip("fastapi")
    from _promote_capsule import make_capsule
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    make_capsule("run-r8", tmp_path / "runs")
    token = "test-token-r8-bypass-store"
    api = create_app(
        capsule_dir=tmp_path / "runs", token=token, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(api) as client:
        yield client, token


def test_serve_answers_503_and_records_the_attempt_when_the_store_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_client: tuple[object, str]
) -> None:
    from novafabric import _paths
    from novafabric.serve import audit as dashboard_audit

    from .test_bypass_audit_fail_closed import _key_cert

    monkeypatch.delenv("NOVA_BYPASS_NOTIFY_FILE", raising=False)
    monkeypatch.delenv("NOVA_BYPASS_NOTIFY_WEBHOOK", raising=False)
    client, token = serve_client
    _, _, key_pem, cert_pem = _key_cert(tmp_path)
    _fail_rename(monkeypatch)

    res = client.post(  # type: ignore[attr-defined]
        f"/api/seal/run-r8/bypass?token={token}",
        json={"reason": REASON, "duration_hours": 2, "key_pem": key_pem, "cert_pem": cert_pem},
        headers={"host": "127.0.0.1:4321"},
    )

    assert res.status_code == 503, res.text
    assert "stored" in res.text.lower()
    assert PromoteBundleStore(_paths.nova_home()).list_bypasses("run-r8") == []
    records = [r for r in dashboard_audit.read_recent(50) if r["action"] == "seal_bypass"]
    assert [r["result"] for r in records] == ["error"]
    assert [r["event_type"] for r in _entries()][-2:] == [
        "seal.bypass.create",
        "seal.bypass.abort",
    ]
    assert os.environ["NOVAFABRIC_AUDIT_LOG_PATH"] == str(resolve_audit_log_path())
