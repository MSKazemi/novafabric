"""Knowledge-graph ingest marks a sealed-then-edited capsule distinctly from an unsealed one.

``capsule_seal_verifies`` answers False both for a capsule that was never sealed and for
one whose ``.seal`` exists but fails verification, so ``POST /api/kg/ingest`` and the
auto-ingest loop ingested a tampered capsule as merely "not verified" — the same as an
unsigned one. Ingest now reads the tri-state (``absent`` | ``valid`` | ``invalid``):

* the edges are never counted seal-verified unless the seal is ``valid``;
* ``/api/kg/ingest`` reports ``seal_status`` and ``tampered`` (additive response fields);
* auto-ingest logs a warning naming the capsule.

A valid seal means the bytes it names are unchanged since sealing; it does not say who
signed them.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

pytest.importorskip("kuzu")

from novafabric.capture.finalize import evidence_digests  # noqa: E402
from novafabric.kg.store import KGStore  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.trust.novaseal import KeyConfig, NovaSeal  # noqa: E402

TOKEN = "kg-tamper-token"
HEADERS = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}

KINDS = [
    "valid", "absent", "tampered_member", "tampered_manifest",
    "corrupt_envelope", "seal_symlink", "seal_regular_file",
]
INVALID_KINDS = [k for k in KINDS if k not in ("valid", "absent")]
# POST /api/kg/ingest already refuses a capsule that holds a top-level link (a .seal link
# included) with an HTTP error before any seal is read; the other paths reach the seal.
ENDPOINT_INVALID_KINDS = [k for k in INVALID_KINDS if k != "seal_symlink"]
CALL = {
    "model_call_id": "m1", "parent_span_id": "agent-span",
    "gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o",
}


@pytest.fixture()
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path, Path]:
    kg = tmp_path / "kg" / "nova_kg.kuzu"
    monkeypatch.setenv("NOVA_KG_PATH", str(kg))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    store = tmp_path / "runs"
    store.mkdir()
    client = TestClient(create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "r.db"))
    return client, store, kg


def _seal_key(work: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kg-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256())
    )
    key_path, cert_path = work / "seal.key", work / "seal.crt"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _make(store: Path, kind: str, work: Path, name: str = "cap") -> Path:
    """A capsule with one model call, in the seal state *kind* describes."""
    capsule = store / name
    capsule.mkdir()
    (capsule / "model-calls.jsonl").write_text(json.dumps(CALL) + "\n")
    (capsule / "capsule.yaml").write_text(yaml.safe_dump({"run_id": name, "status": "success"}))
    if kind == "absent":
        return capsule
    work.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = _seal_key(work)
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    manifest["evidence_digests"] = evidence_digests(capsule)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    bundle = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="", db_path=str(work / "merkle.db"),
    ).seal(manifest)
    seal = capsule / ".seal"
    seal.mkdir()
    (seal / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal / "log-entry.json").write_text(json.dumps(bundle.log_entry))
    if kind == "tampered_member":
        with (capsule / "model-calls.jsonl").open("a") as fh:
            fh.write(json.dumps({**CALL, "model_call_id": "m2"}) + "\n")
    elif kind == "tampered_manifest":
        with (capsule / "capsule.yaml").open("a") as fh:
            fh.write("status_note: edited after sealing\n")
    elif kind == "corrupt_envelope":
        (seal / "manifest.dsse").write_bytes(b"{ not a dsse envelope")
    elif kind == "seal_symlink":
        real = work / "real-seal"
        seal.rename(real)
        os.symlink(real, seal)
    elif kind == "seal_regular_file":
        for child in seal.iterdir():
            child.unlink()
        seal.rmdir()
        seal.write_text("not a directory")
    return capsule


def _ingest(client: TestClient, cap: Path, **extra: object) -> dict:  # type: ignore[type-arg]
    resp = client.post("/api/kg/ingest", headers=HEADERS,
                       json={"capsule_path": cap.name, **extra})
    body = resp.json()
    assert body["ok"] is True, body
    return body  # type: ignore[no-any-return]


@pytest.mark.parametrize("kind", ENDPOINT_INVALID_KINDS)
def test_a_tampered_capsule_is_marked_tampered_and_never_verified(
    stack: tuple[TestClient, Path, Path], tmp_path: Path, kind: str
) -> None:
    client, store, kg = stack
    cap = _make(store, kind, tmp_path / "w")
    body = _ingest(client, cap, verified=True)  # a caller's flag changes nothing
    assert body["seal_status"] == "invalid"
    assert body["tampered"] is True
    assert body["novaseal_verified"] is False
    assert "failed seal verification" in body["note"]
    (edge,) = KGStore(kg).query_agent_models("agent-span")
    assert edge["confidence"] == 0.0


def test_an_unsealed_capsule_stays_visibly_unsigned_not_tampered(
    stack: tuple[TestClient, Path, Path], tmp_path: Path
) -> None:
    client, store, kg = stack
    body = _ingest(client, _make(store, "absent", tmp_path / "w"))
    assert body["seal_status"] == "absent"
    assert body["tampered"] is False
    assert body["novaseal_verified"] is False
    assert "failed seal verification" not in body["note"]
    (edge,) = KGStore(kg).query_agent_models("agent-span")
    assert edge["confidence"] == 0.0


def test_a_validly_sealed_capsule_is_unchanged(
    stack: tuple[TestClient, Path, Path], tmp_path: Path
) -> None:
    client, store, kg = stack
    body = _ingest(client, _make(store, "valid", tmp_path / "w"))
    assert body["seal_status"] == "valid"
    assert body["tampered"] is False
    assert body["novaseal_verified"] is True
    assert body["note"] == ""
    (edge,) = KGStore(kg).query_agent_models("agent-span")
    assert edge["confidence"] == 1.0


def test_a_linked_seal_is_still_refused_by_the_endpoint(
    stack: tuple[TestClient, Path, Path], tmp_path: Path
) -> None:
    client, store, kg = stack
    cap = _make(store, "seal_symlink", tmp_path / "w")
    resp = client.post("/api/kg/ingest", headers=HEADERS, json={"capsule_path": cap.name})
    assert resp.status_code >= 400
    assert not kg.exists() or KGStore(kg).query_agent_models("agent-span") == []


@pytest.mark.parametrize("kind", ENDPOINT_INVALID_KINDS)
def test_absent_and_invalid_are_told_apart(
    stack: tuple[TestClient, Path, Path], tmp_path: Path, kind: str
) -> None:
    client, store, _ = stack
    unsigned = _ingest(client, _make(store, "absent", tmp_path / "w1", name="unsigned"))
    tampered = _ingest(client, _make(store, kind, tmp_path / "w2", name="tampered"))
    assert (unsigned["seal_status"], unsigned["tampered"]) == ("absent", False)
    assert (tampered["seal_status"], tampered["tampered"]) == ("invalid", True)
    assert unsigned["novaseal_verified"] is tampered["novaseal_verified"] is False


def test_the_unsealed_flag_note_is_unchanged(
    stack: tuple[TestClient, Path, Path], tmp_path: Path
) -> None:
    client, store, _ = stack
    body = _ingest(client, _make(store, "absent", tmp_path / "w"), verified=True)
    assert "flag was ignored" in body["note"]
    assert "failed seal verification" not in body["note"]


def test_ingest_all_names_the_tampered_capsules_only(
    stack: tuple[TestClient, Path, Path], tmp_path: Path
) -> None:
    client, store, _ = stack
    _make(store, "absent", tmp_path / "w1", name="unsigned")
    _make(store, "valid", tmp_path / "w2", name="good")
    _make(store, "tampered_manifest", tmp_path / "w3", name="bad")
    _make(store, "seal_symlink", tmp_path / "w4", name="linked")
    assert client.post("/api/kg/init", headers=HEADERS).json()["ok"] is True
    body = client.post("/api/kg/ingest-all", headers=HEADERS, json={}).json()
    assert body["ok"] is True, body
    assert sorted(body["tampered_capsules"]) == ["bad", "linked"]


# -- the auto-ingest loop ----------------------------------------------------------------


def _wait_for_tracked(kg: Path, count: int, timeout: float = 20.0) -> None:
    db = kg.parent / "ingest_tracker.db"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if db.exists():
            conn = sqlite3.connect(db)
            try:
                if conn.execute("SELECT COUNT(*) FROM ingest_state").fetchone()[0] >= count:
                    return
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        time.sleep(0.1)
    raise AssertionError(f"auto-ingest tracked fewer than {count} capsules in {timeout}s")


def test_auto_ingest_warns_about_a_tampered_capsule_and_never_counts_it_verified(
    stack: tuple[TestClient, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, kg = stack
    assert client.post("/api/kg/init", headers=HEADERS).json()["ok"] is True
    _make(store, "tampered_member", tmp_path / "w")
    monkeypatch.setenv("NOVA_KG_INGEST_INTERVAL", "0.05")
    caplog.set_level("INFO")
    with TestClient(create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "r2.db")):
        _wait_for_tracked(kg, 1)
    warned = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "cap" in r.getMessage()
        and "failed seal verification" in r.getMessage()
    ]
    assert warned, [r.getMessage() for r in caplog.records]
    (edge,) = KGStore(kg).query_agent_models("agent-span")
    assert edge["confidence"] == 0.0


def test_auto_ingest_is_quiet_about_an_unsealed_capsule(
    stack: tuple[TestClient, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, kg = stack
    assert client.post("/api/kg/init", headers=HEADERS).json()["ok"] is True
    _make(store, "absent", tmp_path / "w")
    monkeypatch.setenv("NOVA_KG_INGEST_INTERVAL", "0.05")
    caplog.set_level("INFO")
    with TestClient(create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "r2.db")):
        _wait_for_tracked(kg, 1)
    assert not [r for r in caplog.records if "failed seal verification" in r.getMessage()]
