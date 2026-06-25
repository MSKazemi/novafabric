"""Tests for v0.27 registry utility endpoints.

Covers:
  POST /api/validate-spec  — validate asset YAML spec without registering
  GET  /api/report         — generate asset inventory report
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-v027reg"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}

VALID_SPEC_YAML = """\
novafabric_spec_version: "1"
asset_type: prompt
name: test-prompt
version: "1.0.0"
status: development
description: "A test prompt"
spec:
  template: "Analyze this input."
"""

VALID_MODEL_SPEC = """\
novafabric_spec_version: "1"
asset_type: model
name: test-model
version: "1.0.0"
status: development
description: "A test model"
spec:
  framework: pytorch
  artifact_path: s3://models/test-model/1.0.0
"""


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    app = create_app(
        capsule_dir=capsule_dir,
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# POST /api/validate-spec
# ---------------------------------------------------------------------------


def test_validate_spec_valid(client: TestClient) -> None:
    """A fully valid spec YAML returns ok=True and valid=True."""
    resp = client.post(
        f"/api/validate-spec?{TOKEN_Q}",
        json={"spec_yaml": VALID_SPEC_YAML},
        headers=H,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["valid"] is True
    assert data["errors"] == []


def test_validate_spec_model_valid(client: TestClient) -> None:
    """A valid model spec returns ok=True and valid=True."""
    resp = client.post(
        f"/api/validate-spec?{TOKEN_Q}",
        json={"spec_yaml": VALID_MODEL_SPEC},
        headers=H,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["valid"] is True


def test_validate_spec_missing_required_field(client: TestClient) -> None:
    """A spec missing required fields returns valid=False with a non-empty errors list."""
    # Missing novafabric_spec_version, spec block, etc.
    bad_spec = "name: incomplete-model\nasset_type: model\n"
    resp = client.post(
        f"/api/validate-spec?{TOKEN_Q}",
        json={"spec_yaml": bad_spec},
        headers=H,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["valid"] is False
    assert len(data["errors"]) > 0


def test_validate_spec_bad_yaml(client: TestClient) -> None:
    """Malformed YAML returns ok or valid=False with an errors list (does not 500)."""
    bad_yaml = "name: broken\n  bad_indent: [\n"
    resp = client.post(
        f"/api/validate-spec?{TOKEN_Q}",
        json={"spec_yaml": bad_yaml},
        headers=H,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert len(data["errors"]) > 0


def test_validate_spec_missing_body_field(client: TestClient) -> None:
    """Sending an empty spec_yaml returns HTTP 422."""
    resp = client.post(
        f"/api/validate-spec?{TOKEN_Q}",
        json={"spec_yaml": ""},
        headers=H,
    )
    assert resp.status_code == 422


def test_validate_spec_requires_token(client: TestClient) -> None:
    """Omitting the token returns HTTP 401."""
    resp = client.post(
        "/api/validate-spec",
        json={"spec_yaml": VALID_SPEC_YAML},
        headers=H,
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/report
# ---------------------------------------------------------------------------


def test_asset_report_json(client: TestClient) -> None:
    """Requesting json format returns ok=True and non-empty content."""
    resp = client.get(f"/api/report?{TOKEN_Q}&format=json", headers=H)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["format"] == "json"
    assert isinstance(data["content"], str)
    assert len(data["content"]) > 0


def test_asset_report_markdown(client: TestClient) -> None:
    """Requesting markdown format returns ok=True and non-empty content."""
    resp = client.get(f"/api/report?{TOKEN_Q}&format=markdown", headers=H)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["format"] == "markdown"
    assert isinstance(data["content"], str)
    assert len(data["content"]) > 0


def test_asset_report_default_format(client: TestClient) -> None:
    """Omitting the format parameter defaults to json."""
    resp = client.get(f"/api/report?{TOKEN_Q}", headers=H)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["format"] == "json"


def test_asset_report_requires_token(client: TestClient) -> None:
    """Omitting the token returns HTTP 401."""
    resp = client.get("/api/report?format=json", headers=H)
    assert resp.status_code == 401
