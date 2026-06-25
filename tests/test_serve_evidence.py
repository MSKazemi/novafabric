"""Tests for the evidence list/detail/download routes in the experimental serve app."""
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

# The host-header guard requires localhost-style Host values.
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}
TOKEN = "test-token"


@pytest.fixture()
def evidence_dir(tmp_path: Path) -> Path:
    ed = tmp_path / "evidence"
    ed.mkdir()
    return ed


@pytest.fixture()
def sample_bundle(evidence_dir: Path) -> str:
    """Create a minimal evidence bundle ZIP. Returns run_id (= bundle_id = stem)."""
    run_id = "test-run-abc123"
    manifest: dict = {
        "run_id": run_id,
        "created_at": "2026-05-12T10:00:00Z",
        "files": [],
        "attestations": [],
        "signatures": [],
    }
    import hashlib

    work = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(work, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    zip_path = evidence_dir / f"{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
    return run_id


@pytest.fixture()
def client(tmp_path: Path, evidence_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence_dir))
    # Prevent a real novaseal.yaml (from env or ~/.novafabric/) from leaking into tests
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", "/tmp/_novafabric_no_such_seal_config_test.yaml")
    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    return TestClient(app)


def test_list_evidence_empty(client: TestClient, evidence_dir: Path) -> None:
    """Empty directory returns count=0 and empty bundles list."""
    r = client.get("/api/evidence", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["bundles"] == []


def test_list_evidence_with_bundle(client: TestClient, sample_bundle: str) -> None:
    """After adding a valid bundle, list returns it with verified=True."""
    r = client.get("/api/evidence", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    b = data["bundles"][0]
    assert b["bundle_id"] == sample_bundle
    assert b["run_id"] == sample_bundle
    assert b["verified"] is True
    assert b["size_bytes"] > 0
    assert b["timestamp"] == "2026-05-12T10:00:00Z"


def test_get_evidence_detail(client: TestClient, sample_bundle: str) -> None:
    """Detail endpoint returns manifest, empty DSSE fields, and no fingerprint."""
    r = client.get(
        f"/api/evidence/{sample_bundle}",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["bundle_id"] == sample_bundle
    assert data["run_id"] == sample_bundle
    assert data["timestamp"] == "2026-05-12T10:00:00Z"
    assert "manifest" in data
    assert data["manifest"]["run_id"] == sample_bundle
    assert data["signing_key_fingerprint"] is None  # no cert in minimal bundle
    assert isinstance(data["files"], list)
    assert any(f["path"] == "manifest.json" for f in data["files"])


def test_get_evidence_detail_not_found(client: TestClient) -> None:
    """Non-existent bundle_id returns 404."""
    r = client.get(
        "/api/evidence/nonexistent-run",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 404


def test_download_evidence(client: TestClient, sample_bundle: str) -> None:
    """Download route streams the ZIP with Content-Disposition attachment header."""
    r = client.get(
        f"/api/evidence/{sample_bundle}/download",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers.get("content-disposition", "")
    # The content should be a valid ZIP
    import io

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "manifest.json" in zf.namelist()


def test_download_evidence_not_found(client: TestClient) -> None:
    """Download route returns 404 for missing bundle."""
    r = client.get(
        "/api/evidence/no-such-bundle/download",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 404


def test_list_evidence_requires_token(client: TestClient) -> None:
    """Evidence list endpoint rejects requests without a valid token."""
    r = client.get("/api/evidence", headers=LOCALHOST_HEADERS)
    assert r.status_code == 401


def test_evidence_tampered_manifest(
    evidence_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle with a wrong manifest_hash is returned with verified=False."""
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence_dir))
    run_id = "tampered-run-xyz"
    manifest = {
        "run_id": run_id,
        "created_at": "2026-05-12T11:00:00Z",
        "files": [],
        "manifest_hash": "deadbeef" * 8,  # deliberately wrong
    }
    zip_path = evidence_dir / f"{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))

    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    c = TestClient(app)
    r = c.get("/api/evidence", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    bundles = r.json()["bundles"]
    tampered = next((b for b in bundles if b["bundle_id"] == run_id), None)
    assert tampered is not None
    assert tampered["verified"] is False


def test_list_evidence_nonexistent_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When NOVAFABRIC_EVIDENCE_DIR points to a non-existent path, list returns empty."""
    nonexistent = tmp_path / "no_such_dir"
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(nonexistent))
    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    c = TestClient(app)
    r = c.get("/api/evidence", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"bundles": [], "count": 0}


def test_list_evidence_corrupt_zip_handled_gracefully(
    evidence_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt ZIP is listed with verified=False and does not crash the endpoint."""
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence_dir))
    (evidence_dir / "corrupt-run-999.zip").write_bytes(b"not a zip")
    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    c = TestClient(app)
    r = c.get("/api/evidence", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    data = r.json()
    entry = next((b for b in data["bundles"] if b["bundle_id"] == "corrupt-run-999"), None)
    assert entry is not None
    assert entry["verified"] is False


def test_get_evidence_detail_with_dsse_and_cert(
    evidence_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detail endpoint parses DSSE envelope, decodes payload, and extracts cert fingerprint."""
    import base64

    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence_dir))
    run_id = "run-with-dsse"
    statement = {"_type": "https://in-toto.io/Statement/v0.1", "subject": []}
    payload_b64 = base64.b64encode(json.dumps(statement).encode()).decode()
    envelope = {
        "payloadType": "application/vnd.in-toto+json",
        "payload": payload_b64,
        "signatures": [],
    }
    cert_bytes = b"fake-cert-data"

    zip_path = evidence_dir / f"{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"run_id": run_id}))
        zf.writestr("attestations/run.intoto.json", json.dumps(envelope))
        zf.writestr("signatures/run.intoto.json.cert", cert_bytes)

    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    c = TestClient(app)
    r = c.get(f"/api/evidence/{run_id}", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["dsse_statement"]["_type"] == "https://in-toto.io/Statement/v0.1"
    assert data["dsse_envelope"]["payloadType"] == "application/vnd.in-toto+json"
    assert data["signing_key_fingerprint"] is not None


def test_get_evidence_detail_dsse_invalid_payload(
    evidence_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DSSE envelope with a non-base64 payload degrades gracefully (empty statement)."""
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence_dir))
    run_id = "run-bad-payload"
    envelope = {"payloadType": "application/vnd.in-toto+json", "payload": "!!!not-base64!!!"}
    zip_path = evidence_dir / f"{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"run_id": run_id}))
        zf.writestr("attestations/run.intoto.json", json.dumps(envelope))
    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    c = TestClient(app)
    r = c.get(f"/api/evidence/{run_id}", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    assert r.json()["dsse_statement"] == {}


def test_get_evidence_detail_bad_zip_returns_422(
    evidence_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requesting detail for a corrupt ZIP returns 422 Unprocessable Entity."""
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence_dir))
    run_id = "bad-zip-detail"
    (evidence_dir / f"{run_id}.zip").write_bytes(b"garbage bytes")
    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    c = TestClient(app)
    r = c.get(f"/api/evidence/{run_id}", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/evidence/{bundle_id}/verify — DC-1
# ---------------------------------------------------------------------------

def test_verify_evidence_not_found(client: TestClient) -> None:
    """Verify on a missing bundle returns 404."""
    r = client.post(
        "/api/evidence/nonexistent-bundle/verify",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 404


def test_verify_evidence_missing_dsse(client: TestClient, sample_bundle: str) -> None:
    """Bundle without attestations/run.intoto.json → signature_ok=False."""
    r = client.post(
        f"/api/evidence/{sample_bundle}/verify",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["signature_ok"] is False
    assert data["log_integrity_ok"] is None  # NovaSeal not configured in test env
    assert data["seal_available"] is False
    assert len(data["errors"]) >= 1
    assert data["valid"] is False


def test_verify_evidence_valid_signature(
    evidence_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundle with a valid Ed25519 DSSE → signature_ok=True, valid=True."""
    from novafabric.evidence.intoto import dsse_sign, make_intoto_statement
    from novafabric.evidence.signing import LocalSigner, generate_keypair

    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence_dir))
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", "/tmp/_novafabric_no_such_seal_config_test.yaml")

    key_dir = tmp_path / "keys"
    priv_path, _pub_path = generate_keypair(key_dir)
    signer = LocalSigner(priv_path)

    run_id = "signed-run-dc1"
    statement = make_intoto_statement(
        predicate_type="https://novafabric.io/predicates/run/v1",
        subject_name=run_id,
        subject_sha256="a" * 64,
        predicate={"run_id": run_id},
    )
    envelope = dsse_sign(statement, signer)

    zip_path = evidence_dir / f"{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"run_id": run_id}))
        zf.writestr("attestations/run.intoto.json", json.dumps(envelope))
        zf.writestr("signatures/run.cert", signer.public_pem)

    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    c = TestClient(app)
    r = c.post(
        f"/api/evidence/{run_id}/verify",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["signature_ok"] is True
    assert data["timestamp_ok"] is None   # no TSR file in bundle
    assert data["log_integrity_ok"] is None  # NovaSeal not configured in test env
    assert data["seal_available"] is False
    assert data["valid"] is True
    assert data["errors"] == []


def test_verify_evidence_tampered_signature(
    evidence_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundle with a corrupted DSSE signature → signature_ok=False, valid=False."""
    from novafabric.evidence.intoto import dsse_sign, make_intoto_statement
    from novafabric.evidence.signing import LocalSigner, generate_keypair

    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence_dir))
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", "/tmp/_novafabric_no_such_seal_config_test.yaml")

    key_dir = tmp_path / "keys2"
    priv_path, _pub_path = generate_keypair(key_dir)
    signer = LocalSigner(priv_path)

    run_id = "tampered-sig-dc1"
    statement = make_intoto_statement(
        predicate_type="https://novafabric.io/predicates/run/v1",
        subject_name=run_id,
        subject_sha256="b" * 64,
        predicate={"run_id": run_id},
    )
    envelope = dsse_sign(statement, signer)
    # Corrupt the signature bytes
    import base64 as _b64
    bad_sig = _b64.b64encode(b"\xff" * 64).decode()
    envelope["signatures"][0]["sig"] = bad_sig

    zip_path = evidence_dir / f"{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"run_id": run_id}))
        zf.writestr("attestations/run.intoto.json", json.dumps(envelope))
        zf.writestr("signatures/run.cert", signer.public_pem)

    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    c = TestClient(app)
    r = c.post(
        f"/api/evidence/{run_id}/verify",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["signature_ok"] is False
    assert data["valid"] is False
    assert len(data["errors"]) >= 1


def test_verify_evidence_bad_zip_returns_422(
    evidence_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify on a corrupt ZIP returns 422."""
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence_dir))
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", "/tmp/_novafabric_no_such_seal_config_test.yaml")
    run_id = "bad-zip-verify"
    (evidence_dir / f"{run_id}.zip").write_bytes(b"garbage")
    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    c = TestClient(app)
    r = c.post(
        f"/api/evidence/{run_id}/verify",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 422


def test_verify_evidence_requires_token(client: TestClient, sample_bundle: str) -> None:
    """Verify endpoint rejects requests without a valid token."""
    r = client.post(
        f"/api/evidence/{sample_bundle}/verify",
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 401
