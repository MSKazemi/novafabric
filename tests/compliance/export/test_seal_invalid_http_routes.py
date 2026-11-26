"""What the dashboard export routes do with a capsule whose ``.seal`` fails verification.

Documented in docs/api-reference.md and docs/cli-reference.md, so pinned here:

* ``POST /api/compliance/export/ropa`` and ``POST /api/compliance/examiner/{pccp,bagit,
  iso42001}`` refuse: HTTP 409 (the capsule's state conflicts with the request, not a
  server fault) carrying the :class:`CapsuleSealInvalidError` message, and no document;
* a capsule that verifies, or has no seal, still exports (HTTP 200).
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
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert _REFUSAL in detail and "nova verify" in detail
    assert "document" not in resp.json()


_EXAMINER = ["pccp", "bagit", "iso42001"]


def _examiner_post(client: TestClient, fmt: str, run_id: str) -> Any:
    body: dict[str, Any] = {"run_id": run_id}
    if fmt == "iso42001":
        body["period"] = "2023-01-01/2025-12-31"  # the fixture capsules are dated 2024
    return client.post(f"/api/compliance/examiner/{fmt}", params=AUTH, headers=HOST, json=body)


@pytest.mark.parametrize("fmt", _EXAMINER)
@pytest.mark.parametrize("how", ["edit-stream", "edit-manifest", "corrupt-dsse"])
def test_examiner_routes_refuse_an_invalid_seal(
    stack: tuple[TestClient, Path], fmt: str, how: str
) -> None:
    client, store = stack
    cap = _sealed_capsule(store, "cap")
    _damage(cap, how, store.parent)
    resp = _examiner_post(client, fmt, "cap")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail.startswith("examiner export failed:")
    assert _REFUSAL in detail and "nova verify" in detail
    assert resp.json().get("ok") is not True


@pytest.mark.parametrize("fmt", _EXAMINER)
@pytest.mark.parametrize("make", [_sealed_capsule, _unsealed_capsule])
def test_valid_and_absent_seals_still_export_examiner(
    stack: tuple[TestClient, Path], fmt: str, make: Callable[[Path, str], Path]
) -> None:
    client, store = stack
    make(store, "cap")
    resp = _examiner_post(client, fmt, "cap")
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("make", [_sealed_capsule, _unsealed_capsule])
def test_valid_and_absent_seals_still_export_ropa(
    stack: tuple[TestClient, Path], make: Callable[[Path, str], Path]
) -> None:
    client, store = stack
    make(store, "cap")
    resp = _post(client, "/api/compliance/export/ropa", "cap")
    assert resp.status_code == 200
