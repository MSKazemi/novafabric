"""The Sigstore sign and verify routes agree on what is signed: the capsule's own manifest
(review serve-12).

``/api/seal/sigstore/sign`` signed a caller-supplied ``manifest_json`` (default
``{"capsule_id": "<id>"}``) and ``/verify`` checked a re-derived ``{"capsule_id": …}``:
a real signature never verified, and nothing signed was bound to the capsule's
content. Both now use :func:`sigstore_signing_payload` — ``manifest.json``, else
``capsule.json``, else ``capsule.yaml`` — which ``nova verify --backend sigstore`` uses
too.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.serve.app import create_app
from novafabric.trust.novaseal import sigstore_signer
from novafabric.trust.novaseal.sigstore_signer import (
    SigstoreVerificationResult,
    sigstore_signing_payload,
)

TOKEN = "sigstore-bind-token"
HEADERS = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}


class _FakeSigner:
    """Records what it signs; verifies only those exact bytes."""

    signed: list[bytes] = []

    def sign_artifact(self, data: bytes) -> dict[str, Any]:
        _FakeSigner.signed.append(data)
        return {"fake": True, "payload": data.decode("utf-8", "replace")}

    def verify_bundle(self, bundle: dict[str, Any], data: bytes) -> SigstoreVerificationResult:
        return SigstoreVerificationResult(
            valid=bundle.get("payload") == data.decode("utf-8", "replace"),
            identity="ci@example.com", rekor_log_index=1, error=None,
        )


@pytest.fixture()
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sigstore_signer, "SigstoreSigner", _FakeSigner)
    _FakeSigner.signed = []
    store = tmp_path / "runs"
    store.mkdir()
    return TestClient(create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "r.db")), store


def _capsule(store: Path) -> Path:
    return CaptureOrchestrator(base_dir=store).run(command=[sys.executable, "-c", "pass"]).capsule_dir


def test_sign_signs_the_capsules_manifest_and_verify_accepts_it(
    stack: tuple[TestClient, Path]
) -> None:
    client, store = stack
    cap = _capsule(store)
    signed = client.post("/api/seal/sigstore/sign", headers=HEADERS, json={"capsule_id": cap.name})
    assert signed.status_code == 200, signed.text
    assert _FakeSigner.signed == [(cap / "capsule.yaml").read_bytes()]
    verified = client.post("/api/seal/sigstore/verify", headers=HEADERS,
                           json={"capsule_id": cap.name}).json()
    assert verified["valid"] is True, verified


def test_a_changed_manifest_no_longer_verifies(stack: tuple[TestClient, Path]) -> None:
    client, store = stack
    cap = _capsule(store)
    client.post("/api/seal/sigstore/sign", headers=HEADERS, json={"capsule_id": cap.name})
    (cap / "capsule.yaml").write_text((cap / "capsule.yaml").read_text() + "# edited\n")
    verified = client.post("/api/seal/sigstore/verify", headers=HEADERS,
                           json={"capsule_id": cap.name}).json()
    assert verified["valid"] is False


def test_a_caller_supplied_manifest_is_refused(stack: tuple[TestClient, Path]) -> None:
    client, store = stack
    cap = _capsule(store)
    resp = client.post("/api/seal/sigstore/sign", headers=HEADERS,
                       json={"capsule_id": cap.name, "manifest_json": '{"anything": 1}'})
    assert resp.status_code == 422
    assert _FakeSigner.signed == []


def test_signing_an_unknown_capsule_is_404(stack: tuple[TestClient, Path]) -> None:
    client, _ = stack
    resp = client.post("/api/seal/sigstore/sign", headers=HEADERS,
                       json={"capsule_id": "01HZZZZZZZZZZZZZZZZZZZZZZZ"})
    assert resp.status_code == 404


def test_the_payload_prefers_a_seal_manifest(tmp_path: Path) -> None:
    (tmp_path / "capsule.yaml").write_text("run_id: x\n")
    assert sigstore_signing_payload(tmp_path) == ("capsule.yaml", b"run_id: x\n")
    (tmp_path / "manifest.json").write_text("{}")
    assert sigstore_signing_payload(tmp_path) == ("manifest.json", b"{}")
