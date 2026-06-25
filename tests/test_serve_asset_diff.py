"""Tests for GET /api/assets/{name}/diff — asset spec diff endpoint (DC-6)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

VALID_TOKEN = "dc6-asset-diff-test-token-1234"
HEADERS = {"host": "127.0.0.1:4321"}


# ---------- Fixtures ----------

@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    db = tmp_path / "registry.db"
    return {"capsule_dir": capsule_dir, "db": db}


@pytest.fixture
def client(env: dict[str, Path]) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=env["capsule_dir"],
        db_path=env["db"],
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


# YAML spec templates — use `tool` type (optional spec) to keep fixtures simple.
# Two versions of diff-tool with deliberate differences:
#   entrypoint changed from /v1/run → /v2/run
#   description changed
# (spec.* fields are flattened; description lives at the top-level)

_YAML_V1 = """\
novafabric_spec_version: "1"
asset_type: tool
name: diff-tool
version: 1.0.0
status: development
description: diff test tool v1
spec:
  entrypoint: /v1/run
"""

_YAML_V2 = """\
novafabric_spec_version: "1"
asset_type: tool
name: diff-tool
version: 2.0.0
status: development
description: diff test tool v2
spec:
  entrypoint: /v2/run
"""

# Two identical specs registered under different version labels
_YAML_V3_A = """\
novafabric_spec_version: "1"
asset_type: model
name: same-model
version: 1.0.0
status: development
description: identical model
spec:
  framework: pytorch
  artifact_path: /tmp/model
"""

_YAML_V3_B = """\
novafabric_spec_version: "1"
asset_type: model
name: same-model
version: 2.0.0
status: development
description: identical model
spec:
  framework: pytorch
  artifact_path: /tmp/model
"""


def _register(client: TestClient, yaml_text: str) -> None:
    res = client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": yaml_text, "confirmed": True},
    )
    assert res.status_code == 200, f"register failed: {res.text}"


# ---------- Tests ----------

def test_diff_with_differences_returns_200_and_correct_payload(client: TestClient) -> None:
    _register(client, _YAML_V1)
    _register(client, _YAML_V2)

    res = client.get(
        f"/api/assets/diff-tool/diff?from_version=1.0.0&to_version=2.0.0&token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["name"] == "diff-tool"
    assert body["from_version"] == "1.0.0"
    assert body["to_version"] == "2.0.0"
    assert body["identical"] is False

    # description changed
    changed_keys = {r["key"] for r in body["changed"]}
    assert "description" in changed_keys or any("entrypoint" in k for k in changed_keys), (
        f"expected description or entrypoint in changed, got: {body['changed']}"
    )

    # entrypoint changed from /v1/run → /v2/run (flattened as spec.entrypoint)
    entrypoint_rows = [r for r in body["changed"] if "entrypoint" in r["key"]]
    assert len(entrypoint_rows) == 1, (
        f"expected exactly one entrypoint change, got: {body['changed']}"
    )
    assert entrypoint_rows[0]["from"] == "/v1/run"
    assert entrypoint_rows[0]["to"] == "/v2/run"


def test_diff_same_version_identical_returns_identical_true(client: TestClient) -> None:
    """Diffing an asset version against itself must return identical=True."""
    _register(client, _YAML_V3_A)

    res = client.get(
        f"/api/assets/same-model/diff?from_version=1.0.0&to_version=1.0.0&token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["identical"] is True
    assert body["changed"] == []
    assert body["added"] == []
    assert body["removed"] == []


def test_diff_unknown_asset_returns_404(client: TestClient) -> None:
    res = client.get(
        f"/api/assets/nonexistent-asset/diff?from_version=1.0.0&to_version=2.0.0&token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 404, res.text


def test_diff_missing_query_param_returns_422(client: TestClient) -> None:
    # Missing to_version
    res = client.get(
        f"/api/assets/diff-agent/diff?from_version=1.0.0&token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 422

    # Missing from_version
    res = client.get(
        f"/api/assets/diff-agent/diff?to_version=2.0.0&token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 422

    # Missing both
    res = client.get(
        f"/api/assets/diff-agent/diff?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 422


def test_diff_path_traversal_in_name_returns_400(client: TestClient) -> None:
    # URL-encoded ../ in the name path segment
    res = client.get(
        f"/api/assets/..%2F../diff?from_version=1.0.0&to_version=2.0.0&token={VALID_TOKEN}",
        headers=HEADERS,
    )
    # 404 is acceptable if FastAPI path routing rejects the encoded path before our handler
    assert res.status_code in (400, 404)


def test_diff_dotdot_in_name_returns_400(client: TestClient) -> None:
    res = client.get(
        f"/api/assets/..evil../diff?from_version=1.0.0&to_version=2.0.0&token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 400


def test_diff_slash_in_version_returns_400(client: TestClient) -> None:
    res = client.get(
        f"/api/assets/diff-agent/diff?from_version=1.0/0&to_version=2.0.0&token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 400


def test_diff_requires_token(client: TestClient) -> None:
    res = client.get(
        "/api/assets/diff-agent/diff?from_version=1.0.0&to_version=2.0.0",
        headers=HEADERS,
    )
    assert res.status_code == 401
