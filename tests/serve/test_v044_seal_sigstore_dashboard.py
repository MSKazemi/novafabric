"""Tests for v0.44.0 Sigstore keyless-signing dashboard routes.

Covers:
  POST /api/seal/sigstore/sign   — sign a capsule artifact via Sigstore
  POST /api/seal/sigstore/verify — verify a stored Sigstore bundle

See CLAUDE.md § Next implementation steps (cluster-scale) and ADR-0071.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-v044-sigstore"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# POST /api/seal/sigstore/sign
# ---------------------------------------------------------------------------


def test_sigstore_sign_missing_capsule_id_returns_422(client: TestClient) -> None:
    """POST /api/seal/sigstore/sign without capsule_id must return 422."""
    r = client.post(f"/api/seal/sigstore/sign?{TOKEN_Q}", json={}, headers=H)
    assert r.status_code == 422
    body = r.json()
    assert "capsule_id" in body.get("detail", "").lower()


def test_sigstore_sign_no_sigstore_installed_returns_501(
    client: TestClient, tmp_path: Path
) -> None:
    """When the sigstore package is absent the route must return 501."""
    # Patch the import of SigstoreSigner inside the serve module so it raises
    # ImportError, simulating a missing novafabric[sigstore] extra.
    with patch.dict(
        "sys.modules",
        {"novafabric.trust.novaseal.sigstore_signer": None},  # type: ignore[dict-item]
    ):
        r = client.post(
            f"/api/seal/sigstore/sign?{TOKEN_Q}",
            json={"capsule_id": "test-capsule-001"},
            headers=H,
        )
    # The route catches ImportError and returns 501.
    assert r.status_code == 501
    detail = r.json().get("detail", "")
    assert "sigstore" in detail.lower()


# ---------------------------------------------------------------------------
# POST /api/seal/sigstore/verify
# ---------------------------------------------------------------------------


def test_sigstore_verify_missing_capsule_id_returns_422(client: TestClient) -> None:
    """POST /api/seal/sigstore/verify without capsule_id must return 422."""
    r = client.post(f"/api/seal/sigstore/verify?{TOKEN_Q}", json={}, headers=H)
    assert r.status_code == 422
    body = r.json()
    assert "capsule_id" in body.get("detail", "").lower()


def test_sigstore_verify_no_bundle_returns_ok_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify for a capsule with no stored bundle returns ok=False, valid=False."""
    # Point NOVAFABRIC_HOME at a fresh temp dir so no bundle exists.
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nova_home"))

    r = client.post(
        f"/api/seal/sigstore/verify?{TOKEN_Q}",
        json={"capsule_id": "capsule-without-bundle-xyz"},
        headers=H,
    )

    # If sigstore extra is not installed the endpoint returns 501 — skip gracefully.
    if r.status_code == 501:
        pytest.skip("novafabric[sigstore] not installed in this environment")

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["valid"] is False
    assert body["capsule_id"] == "capsule-without-bundle-xyz"
    assert body["error"] is not None
    assert "capsule-without-bundle-xyz" in body["error"]
