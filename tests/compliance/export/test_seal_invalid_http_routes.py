"""What the dashboard export routes do with a capsule whose ``.seal`` fails verification.

Documented in docs/api-reference.md and docs/cli-reference.md, so pinned here:

* ``POST /api/compliance/export/ropa`` and ``POST /api/compliance/examiner/pccp`` refuse:
  HTTP 500 carrying the :class:`CapsuleSealInvalidError` message (the routes' generic
  export-failure handler), and no document;
* a capsule that verifies, or has no seal, still exports (HTTP 200);
* KNOWN GAP, not fixed here: ``POST /api/compliance/examiner/bagit`` (and the CLI
  ``nova export-examiner bagit|iso42001``) run no seal check, so they still export such a
  capsule. The last test records that so a fix has to change it on purpose.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

from .test_seal_invalid_is_refused import _damage, _sealed_capsule, _unsealed_capsule

TOKEN = "seal-http-token"
AUTH = {"token": TOKEN}
HOST = {"host": "127.0.0.1:4321"}
_REFUSAL = "failed seal verification"


@pytest.fixture()
def stack(tmp_path: Path) -> tuple[TestClient, Path]:
    store = tmp_path / "runs"
    store.mkdir()
    app = create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "registry.db")
    return TestClient(app), store


def _post(client: TestClient, path: str, run_id: str) -> Any:
    return client.post(path, params=AUTH, headers=HOST, json={"run_id": run_id})


@pytest.mark.parametrize("how", ["edit-stream", "edit-manifest", "corrupt-dsse"])
def test_ropa_route_refuses_an_invalid_seal(stack: tuple[TestClient, Path], how: str) -> None:
    client, store = stack
    cap = _sealed_capsule(store, "cap")
    _damage(cap, how, store.parent)
    resp = _post(client, "/api/compliance/export/ropa", "cap")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert _REFUSAL in detail and "nova verify" in detail
    assert "document" not in resp.json()


@pytest.mark.parametrize("how", ["edit-stream", "edit-manifest", "corrupt-dsse"])
def test_pccp_route_refuses_an_invalid_seal(stack: tuple[TestClient, Path], how: str) -> None:
    client, store = stack
    cap = _sealed_capsule(store, "cap")
    _damage(cap, how, store.parent)
    resp = _post(client, "/api/compliance/examiner/pccp", "cap")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail.startswith("examiner export failed:")
    assert _REFUSAL in detail and "nova verify" in detail


@pytest.mark.parametrize("route", ["/api/compliance/export/ropa", "/api/compliance/examiner/pccp"])
@pytest.mark.parametrize("make", [_sealed_capsule, _unsealed_capsule])
def test_valid_and_absent_seals_still_export(
    stack: tuple[TestClient, Path], route: str, make: Callable[[Path, str], Path]
) -> None:
    client, store = stack
    make(store, "cap")
    resp = _post(client, route, "cap")
    assert resp.status_code == 200


def test_known_gap_bagit_route_does_not_check_the_seal(stack: tuple[TestClient, Path]) -> None:
    """Known gap, not fixed here: bagit exports a capsule whose seal fails verification."""
    client, store = stack
    cap = _sealed_capsule(store, "cap")
    _damage(cap, "edit-stream", store.parent)
    resp = _post(client, "/api/compliance/examiner/bagit", "cap")
    assert resp.status_code == 200
