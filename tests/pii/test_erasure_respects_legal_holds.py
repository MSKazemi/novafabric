"""A legal hold stops GDPR crypto-shredding (Art. 17(3)(e)), on every surface.

Attack (adversarial review, 2026-10-22): with an indefinite legal hold on the
registry, ``nova pii erase user-42 --retention-months 0`` destroyed the DEK that
protects the held capsule's PII and exited 0. ``POST /api/compliance/erasure/
request`` and ``POST /api/compliance/pii/erase`` did not read holds either.

Owner decision: GDPR Art. 17(3)(e) exempts data needed for the establishment,
exercise or defence of legal claims, so a held capsule is not erased. A subject's
DEK protects every capsule of that subject at once and holds are registry-global
today, so while any hold is active the DEK is retained: the outcome is ``held``,
the receipt and the audit log name the holds and the held capsules, the CLI exits
3 and the HTTP routes answer 409. There is no override flag.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.pii.dek import DEKStore
from novafabric.retention.holds import place_hold, release_hold

runner = CliRunner()
SUBJECT = "user-42"
RUN = "01M5JDD2Y0VKD0XK5YZ4CZNND8"
TOKEN = "erasure-hold-token-0123456789abcdef"
H = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / "capsules" / RUN).mkdir(parents=True)
    (h / "capsules" / RUN / "redaction_manifest.json").write_text(
        json.dumps({"capsule_id": RUN})
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(h))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("NOVA_AI_ACT_RETENTION_MONTHS", "0")
    store = DEKStore(h / "dek.db")
    store.get_or_create_dek(SUBJECT)
    store.close()
    return h


def _dek_exists(home: Path) -> bool:
    store = DEKStore(home / "dek.db")
    try:
        return store.get_dek(SUBJECT) is not None
    finally:
        store.close()


def _audit(tmp_path: Path) -> list[dict[str, Any]]:
    path = Path(os.environ["NOVAFABRIC_AUDIT_LOG_PATH"])
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# DEKStore — the one erasure code path every surface calls
# ---------------------------------------------------------------------------


def test_store_retains_the_dek_of_a_held_subject(home: Path) -> None:
    from novafabric.pii.dek import ErasureHeldReceipt

    hold = place_hold("default", "litigation", actor="legal")
    store = DEKStore(home / "dek.db")
    try:
        result = store.erase_subject(SUBJECT, [RUN], retention_months=0)
    finally:
        store.close()
    assert isinstance(result, ErasureHeldReceipt)
    assert result.dek_retained is True
    assert [c.capsule_id for c in result.held_capsules] == [RUN]
    assert result.held_capsules[0].hold_ids == [hold.hold_id]
    assert result.holds[0].reason == "litigation"
    assert result.legal_basis.startswith("GDPR Art.17(3)(e)")
    assert _dek_exists(home)


def test_store_erases_once_the_hold_is_released(home: Path) -> None:
    from novafabric.pii.dek import ErasureReceipt

    hold = place_hold("default", "litigation", actor="legal")
    release_hold(hold.hold_id, actor="legal")
    store = DEKStore(home / "dek.db")
    try:
        result = store.erase_subject(SUBJECT, [RUN], retention_months=0)
    finally:
        store.close()
    assert isinstance(result, ErasureReceipt)
    assert not _dek_exists(home)


def test_a_corrupt_hold_ledger_fails_closed(home: Path) -> None:
    from novafabric.pii.dek import ErasureHeldReceipt

    ledger = home / "registries" / "default" / "holds.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{not json\n")
    store = DEKStore(home / "dek.db")
    try:
        result = store.erase_subject(SUBJECT, [RUN], retention_months=0)
    finally:
        store.close()
    assert isinstance(result, ErasureHeldReceipt)
    assert _dek_exists(home)


# ---------------------------------------------------------------------------
# CLI: nova pii erase
# ---------------------------------------------------------------------------


def test_cli_erase_under_a_hold_exits_3_and_keeps_the_dek(
    home: Path, tmp_path: Path
) -> None:
    hold = place_hold("default", "litigation", actor="legal")
    receipt_path = tmp_path / "receipt.json"
    res = runner.invoke(
        app,
        ["pii", "erase", SUBJECT, "--retention-months", "0", "--output", str(receipt_path)],
    )
    assert isinstance(res.exception, SystemExit), res.exception
    assert res.exit_code == 3, res.output
    assert "legal hold" in res.output
    assert _dek_exists(home)
    receipt = json.loads(receipt_path.read_text())
    assert receipt["dek_retained"] is True
    assert receipt["held_capsules"][0]["capsule_id"] == RUN
    assert receipt["held_capsules"][0]["hold_ids"] == [hold.hold_id]
    held = [e for e in _audit(tmp_path) if e["event_type"] == "erasure.request"]
    assert held and held[-1]["details"]["state"] == "HELD"
    assert held[-1]["details"]["hold_ids"] == [hold.hold_id]
    assert SUBJECT not in json.dumps(held[-1])  # hash-only audit


# ---------------------------------------------------------------------------
# HTTP: /api/compliance/erasure/request and /api/compliance/pii/erase
# ---------------------------------------------------------------------------


@pytest.fixture
def client(home: Path, tmp_path: Path) -> Iterator[Any]:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    app_ = create_app(
        token=TOKEN, capsule_dir=home / "capsules", db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app_) as c:
        yield c


def test_rest_erasure_request_under_a_hold_is_409_held(home: Path, client: Any) -> None:
    hold = place_hold("default", "litigation", actor="legal")
    r = client.post(
        "/api/compliance/erasure/request",
        headers=H,
        json={"subject_id": SUBJECT, "capsule_ids": [RUN], "confirmed": True},
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["erased"] is False
    assert body["error"] == "legal_hold_active"
    assert body["request"]["state"] == "HELD"
    assert body["request"]["receipt"]["held_capsules"][0]["hold_ids"] == [hold.hold_id]
    assert _dek_exists(home)


def test_rest_pii_erase_under_a_hold_is_409(home: Path, client: Any) -> None:
    place_hold("default", "litigation", actor="legal")
    r = client.post(
        "/api/compliance/pii/erase",
        headers=H,
        json={"subject_id": SUBJECT, "capsule_ids": [RUN], "retention_months": 0},
    )
    assert r.status_code == 409, r.text
    assert r.json()["erased"] is False
    assert _dek_exists(home)
