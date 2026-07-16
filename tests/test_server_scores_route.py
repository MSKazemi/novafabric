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

"""Tests for ``POST /v0/capsules/{run_id}/scores`` on the multi-user server (ADR-0119 P3).

The RBAC-gated optional REST surface over the shared submission core: writer role
required (the ``scores:write`` capability), principal recorded in the ``submission``
block, ADR-0017 error envelope, append-only + idempotent semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.capture._ulid import new_ulid  # noqa: E402
from novafabric.eval.scores import SCORES_FILENAME, read_scores  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.auth import AuthContext, verify_token  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402

RUN_ID = "01TEST00000000000000000001"
_SPAN = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_CARD = "sha256:" + "60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    cdir = base / RUN_ID
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.safe_dump({
        "schema_version": "0.1.0",
        "run_id": RUN_ID,
        "created_at": "2026-07-15T00:00:00+00:00",
        "status": "success",
    }))
    (cdir / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "span_digest": _SPAN}) + "\n"
    )
    return base


@pytest.fixture
def client(tmp_path: Path, capsule_dir: Path) -> TestClient:
    from novafabric.server import deps

    # ADR-0184: anonymous admin now requires the explicit insecure opt-out.
    app = create_app(ServerConfig(db_path=str(tmp_path / "server.db"), insecure_no_auth=True))
    app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
    return TestClient(app, raise_server_exceptions=False)


def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "answer_correct",
        "value": True,
        "value_type": "boolean",
        "source": "judge",
        "evaluator_id": "ci://acme/repo#judge@v3",
        "subject": _SPAN,
        "eval_card_digest": _CARD,
    }
    base.update(over)
    return base


def _scores_path(capsule_dir: Path) -> Path:
    return capsule_dir / RUN_ID / SCORES_FILENAME


def test_post_appends_with_principal_attribution(
    client: TestClient, capsule_dir: Path
) -> None:
    resp = client.post(f"/v0/capsules/{RUN_ID}/scores", json=_body())
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["idempotent_replay"] is False
    assert data["submission"]["scope"] == "scores:write"
    assert data["submission"]["principal"] == "local"  # no-auth local mode principal
    scores = read_scores(_scores_path(capsule_dir))
    assert len(scores) == 1
    assert scores[0].evaluator_id == "ci://acme/repo#judge@v3"


def test_reader_role_is_forbidden(client: TestClient, capsule_dir: Path) -> None:
    client.app.dependency_overrides[verify_token] = lambda: AuthContext(  # type: ignore[attr-defined]
        subject="reader@example.com", roles=["reader"]
    )
    resp = client.post(f"/v0/capsules/{RUN_ID}/scores", json=_body())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert not _scores_path(capsule_dir).exists()


def test_unknown_capsule_is_404(client: TestClient) -> None:
    resp = client.post("/v0/capsules/01TEST00000000000000000099/scores", json=_body())
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_dangling_subject_is_404(client: TestClient, capsule_dir: Path) -> None:
    resp = client.post(
        f"/v0/capsules/{RUN_ID}/scores", json=_body(subject="sha256:" + "ab" * 32)
    )
    assert resp.status_code == 404
    assert not _scores_path(capsule_dir).exists()


def test_malformed_body_is_400(client: TestClient, capsule_dir: Path) -> None:
    resp = client.post(
        f"/v0/capsules/{RUN_ID}/scores", json=_body(value=0.5)  # bool type, numeric value
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"
    assert not _scores_path(capsule_dir).exists()


def test_idempotent_replay_and_collision(client: TestClient, capsule_dir: Path) -> None:
    key = new_ulid()
    assert client.post(f"/v0/capsules/{RUN_ID}/scores", json=_body(score_id=key)).status_code == 201
    replay = client.post(f"/v0/capsules/{RUN_ID}/scores", json=_body(score_id=key))
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    collision = client.post(
        f"/v0/capsules/{RUN_ID}/scores", json=_body(score_id=key, value=False)
    )
    assert collision.status_code == 409
    assert collision.json()["error"]["code"] == "idempotency_conflict"
    assert len(read_scores(_scores_path(capsule_dir))) == 1


def test_supersedes_unknown_target_is_422(client: TestClient, capsule_dir: Path) -> None:
    resp = client.post(
        f"/v0/capsules/{RUN_ID}/scores", json=_body(supersedes=new_ulid())
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "supersedes_not_found"
    assert not _scores_path(capsule_dir).exists()
