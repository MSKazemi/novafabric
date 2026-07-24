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
"""REST erasure execution tests (ADR-0210 P1).

Pins the *real* behavior that replaced the v0.18.0 silent no-op stubs:
POST /api/compliance/erasure/request persists a queue row in
$NOVAFABRIC_HOME/erasure.db and executes it synchronously through
DEKStore.erase_subject; GET /api/compliance/erasure/status returns the
persisted truth. The old stub contract (always-PENDING, always-[],
"informational stub" note) is gone — these tests fail if it comes back.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterator

import pytest

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.pii import erasure_queue as eq  # noqa: E402
from novafabric.pii.dek import DEKStore  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"
SUBJECT = "seeded-erasure-subject@example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def nova_home() -> Path:
    """The hermetic NOVAFABRIC_HOME the endpoints resolve at request time."""
    return Path(os.environ["NOVAFABRIC_HOME"])


@pytest.fixture(autouse=True)
def _isolated_audit_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Keep the hash-chained audit log out of ~/.local/share during tests."""
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.pii.erasure_queue.AUDIT_LOG_PATH", path)
    return path


@pytest.fixture
def audit_log_path(_isolated_audit_log: Path) -> Path:
    return _isolated_audit_log


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    return base


@pytest.fixture
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


def _seed_dek(home: Path, subject: str = SUBJECT) -> None:
    store = DEKStore(home / "dek.db")
    store.get_or_create_dek(subject)
    store.close()


def _dek_exists(home: Path, subject: str = SUBJECT) -> bool:
    store = DEKStore(home / "dek.db")
    try:
        return store.get_dek(subject) is not None
    finally:
        store.close()


def _post(client: TestClient, body: dict):  # -> httpx.Response
    return client.post(
        f"/api/compliance/erasure/request?{TOKEN_Q}", json=body, headers=HEADERS
    )


def _status(client: TestClient, **params: str) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    sep = "&" if query else ""
    res = client.get(
        f"/api/compliance/erasure/status?{TOKEN_Q}{sep}{query}", headers=HEADERS
    )
    assert res.status_code == 200
    return res.json()


# ---------------------------------------------------------------------------
# Happy path: real erasure with receipt
# ---------------------------------------------------------------------------


def test_confirmed_request_actually_erases_dek_with_receipt(
    client: TestClient,
    nova_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    _seed_dek(nova_home)
    assert _dek_exists(nova_home)

    res = _post(
        client,
        {"subject_id": SUBJECT, "capsule_ids": ["cap-9"], "confirmed": True},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    request = body["request"]
    assert request["state"] == "COMPLETED"
    # The receipt records what was erased and is hash-verifiable.
    assert request["receipt"]["subject_id"] == SUBJECT
    assert request["receipt"]["method"] == "aes-256-gcm-dek-destruction"
    assert request["receipt"]["capsule_ids_affected"] == ["cap-9"]
    assert request["receipt_sha256"] == eq.receipt_sha256_of(request["receipt"])
    # Top level of the row view is hash-only.
    assert "subject_id" not in request
    assert request["subject_sha256"] == eq.subject_sha256(SUBJECT)
    # The DEK is actually, provably gone — not a stub.
    assert not _dek_exists(nova_home)
    # The queue row is persisted in the dedicated erasure.db (not registry.db).
    assert (nova_home / "erasure.db").exists()
    # /status returns the persisted truth.
    status = _status(client)
    assert len(status["requests"]) == 1
    assert status["requests"][0]["request_id"] == request["request_id"]
    assert status["requests"][0]["state"] == "COMPLETED"


def test_old_stub_contract_is_gone(client: TestClient, nova_home: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """The v0.18.0 stub returned always-PENDING with an 'informational stub' note."""
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    _seed_dek(nova_home)
    res = _post(client, {"subject_id": SUBJECT, "confirmed": True})
    assert res.status_code == 200
    body = res.json()
    assert "note" not in body  # stub self-description removed
    assert body["request"]["state"] != "PENDING"  # requests reach a terminal state
    status = _status(client)
    assert "note" not in status  # the stale ClickHouse/S3 note is deleted
    assert status["requests"] != []  # status is no longer hardwired to []


# ---------------------------------------------------------------------------
# Gates: confirmed / flag / validation
# ---------------------------------------------------------------------------


def test_unconfirmed_request_is_400_and_mutates_nothing(
    client: TestClient, nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    _seed_dek(nova_home)

    for body in ({"subject_id": SUBJECT}, {"subject_id": SUBJECT, "confirmed": False}):
        res = _post(client, body)
        assert res.status_code == 400
        assert "confirmation required" in res.json()["detail"]

    # No mutation: DEK intact, no queue row was created.
    assert _dek_exists(nova_home)
    assert not (nova_home / "erasure.db").exists()
    assert _status(client)["requests"] == []


def test_cap003_disabled_is_fail_closed_409(
    client: TestClient, nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAP003_ENABLED", "false")
    _seed_dek(nova_home)
    res = _post(client, {"subject_id": SUBJECT, "confirmed": True})
    assert res.status_code == 409
    assert res.json() == {"error": "cap003_disabled", "cap003_enabled": False}
    assert _dek_exists(nova_home)
    # /status remains readable with the flag surfaced.
    status = _status(client)
    assert status["cap003_enabled"] is False
    assert status["requests"] == []


def test_empty_subject_id_is_422(client: TestClient) -> None:
    res = _post(client, {"subject_id": "   ", "confirmed": True})
    assert res.status_code == 422


def test_missing_subject_id_is_422(client: TestClient) -> None:
    res = _post(client, {"confirmed": True})
    assert res.status_code == 422


def test_unknown_body_fields_rejected_422(client: TestClient) -> None:
    # run_id was a stub-era artifact; the spec body is closed (extra=forbid).
    res = _post(client, {"run_id": "01RUN", "confirmed": True})
    assert res.status_code == 422


def test_requires_token(client: TestClient) -> None:
    res = client.post(
        "/api/compliance/erasure/request",
        json={"subject_id": SUBJECT, "confirmed": True},
        headers=HEADERS,
    )
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Outcomes: deferred / not-found / already-erased / machinery failure
# ---------------------------------------------------------------------------


def test_retention_window_subject_is_deferred(
    client: TestClient, nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "6")
    _seed_dek(nova_home)  # fresh DEK — inside the window
    res = _post(client, {"subject_id": SUBJECT, "confirmed": True})
    assert res.status_code == 200
    request = res.json()["request"]
    assert request["state"] == "DEFERRED"
    assert "earliest_erasure_at" in request["receipt"]
    assert _dek_exists(nova_home)  # nothing destroyed


def test_unknown_subject_is_failed_subject_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    res = _post(client, {"subject_id": "never-registered@example.com", "confirmed": True})
    assert res.status_code == 200  # request received, executed, honestly recorded
    request = res.json()["request"]
    assert request["state"] == "FAILED"
    assert request["error_class"] == "subject_not_found"
    assert request["receipt"]["kind"] == "error"


def test_repeat_after_completion_is_already_erased(
    client: TestClient, nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    _seed_dek(nova_home)
    first = _post(client, {"subject_id": SUBJECT, "confirmed": True}).json()["request"]
    assert first["state"] == "COMPLETED"

    second = _post(client, {"subject_id": SUBJECT, "confirmed": True}).json()["request"]
    assert second["state"] == "COMPLETED"
    assert second["request_id"] != first["request_id"]
    assert second["receipt"]["kind"] == "already_erased"
    assert second["receipt"]["prior_request_id"] == first["request_id"]


def test_machinery_failure_yields_failed_row_with_error_receipt(
    client: TestClient, nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BrokenStore:
        def erase_subject(self, **_kwargs: object) -> None:
            raise RuntimeError(f"i/o error while shredding {SUBJECT}")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "novafabric.pii.erasure_queue.open_dek_store", lambda _home: _BrokenStore()
    )
    res = _post(client, {"subject_id": SUBJECT, "confirmed": True})
    assert res.status_code == 200
    request = res.json()["request"]
    assert request["state"] == "FAILED"
    assert request["error_class"] == "RuntimeError"
    assert SUBJECT not in json.dumps(request)  # sanitized error surfaces
    # Fail-closed: the persisted row is FAILED, never silently PENDING.
    status = _status(client)
    assert status["requests"][0]["state"] == "FAILED"


# ---------------------------------------------------------------------------
# Idempotency / crash recovery (ADR-0210 D4)
# ---------------------------------------------------------------------------


def test_duplicate_post_reattaches_to_pending_row(
    client: TestClient, nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row orphaned in PENDING (crash between commit and execute) is re-attached."""
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    _seed_dek(nova_home)
    # Simulate the crash: PENDING row committed, process died before execution.
    q = eq.open_erasure_queue(nova_home)
    orphan = q.create_request(subject_id=SUBJECT, capsule_ids=["cap-x"])
    q.close()

    res = _post(client, {"subject_id": SUBJECT, "confirmed": True})
    assert res.status_code == 200
    body = res.json()
    assert body["reattached"] is True
    assert body["request"]["request_id"] == orphan.request_id  # same row, no duplicate
    assert body["request"]["state"] == "COMPLETED"
    assert not _dek_exists(nova_home)
    assert len(_status(client)["requests"]) == 1


# ---------------------------------------------------------------------------
# Persistence across app restarts
# ---------------------------------------------------------------------------


def test_status_survives_app_restart(
    client: TestClient,
    capsule_dir: Path,
    tmp_path: Path,
    nova_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    _seed_dek(nova_home)
    request = _post(client, {"subject_id": SUBJECT, "confirmed": True}).json()["request"]

    # Fresh app instance, same NOVAFABRIC_HOME — the queue is durable.
    app2 = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app2) as c2:
        status = _status(c2)
    assert [r["request_id"] for r in status["requests"]] == [request["request_id"]]
    assert status["requests"][0]["state"] == "COMPLETED"
    assert status["requests"][0]["receipt_sha256"] == request["receipt_sha256"]


def test_status_filters_by_subject_and_empty_db_is_honest(
    client: TestClient, nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No erasure.db at all → honest empty, never a 500.
    assert not (nova_home / "erasure.db").exists()
    assert _status(client)["requests"] == []

    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    _seed_dek(nova_home, "s-one")
    _seed_dek(nova_home, "s-two")
    _post(client, {"subject_id": "s-one", "confirmed": True})
    _post(client, {"subject_id": "s-two", "confirmed": True})

    all_rows = _status(client)["requests"]
    assert len(all_rows) == 2
    filtered = _status(client, subject_id="s-one")["requests"]
    assert len(filtered) == 1
    assert filtered[0]["subject_sha256"] == eq.subject_sha256("s-one")
    assert _status(client, limit="1")["requests"][0] == all_rows[0]  # newest first


# ---------------------------------------------------------------------------
# Dual audit surfaces + hash-only logging (normative)
# ---------------------------------------------------------------------------


def test_both_audit_surfaces_receive_hash_only_events(
    client: TestClient,
    nova_home: Path,
    audit_log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    _seed_dek(nova_home)
    with caplog.at_level(logging.DEBUG):
        res = _post(client, {"subject_id": SUBJECT, "confirmed": True})
    assert res.status_code == 200
    request = res.json()["request"]
    subject_hash = eq.subject_sha256(SUBJECT)

    # 1) Layer B dashboard audit (serve/audit.py JSONL under NOVAFABRIC_HOME).
    dashboard_audit = nova_home / "dashboard-audit.jsonl"
    assert dashboard_audit.exists()
    lines = [json.loads(x) for x in dashboard_audit.read_text().splitlines() if x]
    entries = [e for e in lines if e["action"] == "erasure_request"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["args"]["subject_sha256"] == subject_hash
    assert entry["args"]["request_id"] == request["request_id"]
    assert entry["args"]["state"] == "COMPLETED"
    assert entry["result"] == "ok"
    assert entry["cli_equivalent"] == f"nova pii erase sha256:{subject_hash[:12]}"
    assert SUBJECT not in json.dumps(entry)  # hash-only args, normative

    # 2) Hash-chained AuditLog (ADR-0191 SIEM path).
    from novafabric.audit import AuditLog

    chained = [
        json.loads(x) for x in audit_log_path.read_text().splitlines() if x
    ]
    erasure_events = [e for e in chained if e["event_type"] == "erasure.request"]
    assert len(erasure_events) == 1
    event = erasure_events[0]
    assert event["details"]["subject_sha256"] == subject_hash
    assert event["details"]["state"] == "COMPLETED"
    assert event["details"]["receipt_sha256"] == request["receipt_sha256"]
    assert SUBJECT not in json.dumps(event)
    assert AuditLog(audit_log_path).verify() == []  # chain intact

    # 3) Seeded-grep: raw subject never in any structured log record.
    for record in caplog.records:
        blob = record.getMessage() + repr(vars(record))
        assert SUBJECT not in blob, f"raw subject_id leaked into log: {blob[:200]}"


def test_audit_log_failure_is_non_fatal_after_real_erase(
    client: TestClient, nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hash-chained audit append failure never 500s after a real erasure."""
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    _seed_dek(nova_home)

    def _boom() -> Path:
        raise RuntimeError("audit volume unavailable")

    monkeypatch.setattr(
        "novafabric.pii.erasure_queue.erasure_audit_log_path", _boom
    )
    res = _post(client, {"subject_id": SUBJECT, "confirmed": True})
    assert res.status_code == 200
    assert res.json()["request"]["state"] == "COMPLETED"
    assert not _dek_exists(nova_home)  # the erasure itself happened


def test_failed_request_audits_error_result(
    client: TestClient, nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    res = _post(client, {"subject_id": "ghost@example.com", "confirmed": True})
    assert res.json()["request"]["state"] == "FAILED"
    dashboard_audit = nova_home / "dashboard-audit.jsonl"
    entries = [
        json.loads(x)
        for x in dashboard_audit.read_text().splitlines()
        if x and '"erasure_request"' in x
    ]
    assert entries[-1]["result"] == "error"
    assert entries[-1]["error"] == "subject_not_found"
