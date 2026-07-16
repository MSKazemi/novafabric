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

"""Serve-dashboard tests for ``POST /api/runs/{run_id}/scores`` (ADR-0119).

Token-gated, audit-logged, append-only ingest over the shared submission core:
happy path lands in scores.jsonl with provenance; validation rejections write
nothing; unknown run 404; auth failure 401; idempotent replay 200; resubmission
appends, never overwrites.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.capture._ulid import new_ulid  # noqa: E402
from novafabric.eval.scores import SCORES_FILENAME, read_scores  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
RUN_ID = "01TEST00000000000000000001"
_SPAN = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_CARD = "sha256:" + "60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"
_DANGLING = "sha256:" + "ab" * 32


@pytest.fixture(autouse=True)
def iso_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nfhome"))


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
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "answer_correct",
        "value": 0.87,
        "value_type": "numeric",
        "source": "code",
        "evaluator_id": "ci://acme/repo#judge@v3",
        "subject": _SPAN,
        "eval_card_digest": _CARD,
    }
    base.update(over)
    return base


def _post(client: TestClient, body: dict[str, Any], run_id: str = RUN_ID):
    return client.post(
        f"/api/runs/{run_id}/scores?token={VALID_TOKEN}", json=body, headers=HEADERS
    )


def _scores_path(capsule_dir: Path) -> Path:
    return capsule_dir / RUN_ID / SCORES_FILENAME


def test_post_appends_score_with_provenance(
    client: TestClient, capsule_dir: Path
) -> None:
    resp = _post(client, _body())
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["idempotent_replay"] is False
    assert data["config_bound"] is False
    assert data["score"]["evaluator_id"] == "ci://acme/repo#judge@v3"
    assert data["submission"]["scope"] == "scores:write"
    assert data["submission"]["principal"].startswith("token:")
    scores = read_scores(_scores_path(capsule_dir))
    assert len(scores) == 1
    assert scores[0].score_id == data["score"]["score_id"]


def test_post_is_audit_logged(client: TestClient, capsule_dir: Path) -> None:
    from novafabric.serve import audit

    _post(client, _body())
    actions = [r["action"] for r in audit.read_recent()]
    assert "score_submit" in actions


def test_missing_token_is_unauthorized(client: TestClient, capsule_dir: Path) -> None:
    resp = client.post(f"/api/runs/{RUN_ID}/scores", json=_body(), headers=HEADERS)
    assert resp.status_code == 401
    assert not _scores_path(capsule_dir).exists()


def test_unknown_run_is_404(client: TestClient, capsule_dir: Path) -> None:
    resp = _post(client, _body(), run_id="01TEST00000000000000000099")
    assert resp.status_code == 404


def test_dangling_subject_is_404_nothing_written(
    client: TestClient, capsule_dir: Path
) -> None:
    resp = _post(client, _body(subject=_DANGLING))
    assert resp.status_code == 404
    assert not _scores_path(capsule_dir).exists()


def test_malformed_body_is_400(client: TestClient, capsule_dir: Path) -> None:
    resp = _post(client, _body(value="yes", value_type="boolean"))
    assert resp.status_code == 400
    resp = _post(client, _body(unknown_key="x"))
    assert resp.status_code == 400
    assert not _scores_path(capsule_dir).exists()


def test_config_violation_is_422(client: TestClient, capsule_dir: Path, tmp_path: Path) -> None:
    from novafabric.eval.score_config import ScoreRange
    from novafabric.eval.score_config_catalog import register_config
    from novafabric.eval.scores import ScoreValueType

    register_config(
        name="answer_correct",
        value_type=ScoreValueType.NUMERIC,
        description="0..1 correctness.",
        range_=ScoreRange(min=0.0, max=1.0),
        db_path=tmp_path / "registry.db",
    )
    assert _post(client, _body(value=1.5)).status_code == 422
    assert not _scores_path(capsule_dir).exists()
    ok = _post(client, _body(value=0.5))
    assert ok.status_code == 201
    assert ok.json()["config_bound"] is True


def test_idempotent_replay_is_200_no_second_line(
    client: TestClient, capsule_dir: Path
) -> None:
    key = new_ulid()
    first = _post(client, _body(score_id=key))
    assert first.status_code == 201
    replay = _post(client, _body(score_id=key))
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert len(read_scores(_scores_path(capsule_dir))) == 1


def test_idempotency_collision_is_409(client: TestClient, capsule_dir: Path) -> None:
    key = new_ulid()
    assert _post(client, _body(score_id=key)).status_code == 201
    assert _post(client, _body(score_id=key, value=0.11)).status_code == 409
    assert len(read_scores(_scores_path(capsule_dir))) == 1


def test_supersedes_appends_never_overwrites(
    client: TestClient, capsule_dir: Path
) -> None:
    first = _post(client, _body(value=0.4))
    prior_id = first.json()["score"]["score_id"]
    raw_before = _scores_path(capsule_dir).read_text().splitlines()

    correction = _post(client, _body(value=0.9, supersedes=prior_id))
    assert correction.status_code == 201

    raw_after = _scores_path(capsule_dir).read_text().splitlines()
    assert raw_after[: len(raw_before)] == raw_before  # prior line byte-identical
    assert len(raw_after) == len(raw_before) + 1
    assert read_scores(_scores_path(capsule_dir))[1].supersedes == prior_id


def test_supersedes_unknown_target_is_422(
    client: TestClient, capsule_dir: Path
) -> None:
    resp = _post(client, _body(supersedes=new_ulid()))
    assert resp.status_code == 422
    assert not _scores_path(capsule_dir).exists()
