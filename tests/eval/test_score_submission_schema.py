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

"""Golden-fixture tests for the graduated ADR-0119 submission schemas.

Every fixture under ``tests/fixtures/score-submission-api/`` behaves as its
filename asserts (``*-invalid-*`` reject, others validate) under a closed
Draft 2020-12 validator with format checking — the spec's acceptance criterion.
Also pins the additive-only ``supersedes`` change on the shipped Score schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests" / "fixtures" / "score-submission-api"
_REQUEST_SCHEMA = json.loads(
    (_ROOT / "schemas" / "score-submission-request.schema.json").read_text()
)
_RESPONSE_SCHEMA = json.loads(
    (_ROOT / "schemas" / "score-submission-response.schema.json").read_text()
)
_SCORE_SCHEMA = json.loads((_ROOT / "schemas" / "score-v1.schema.json").read_text())


def _fixtures(prefix: str) -> list[Path]:
    found = sorted(_FIXTURES.glob(f"{prefix}-*.json"))
    assert found, f"no {prefix} fixtures found in {_FIXTURES}"
    return found


def _validator(schema: dict) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


@pytest.mark.parametrize("path", _fixtures("request"), ids=lambda p: p.stem)
def test_request_fixture_behaves_as_named(path: Path) -> None:
    data = json.loads(path.read_text())
    errors = list(_validator(_REQUEST_SCHEMA).iter_errors(data))
    if "-invalid-" in path.name:
        assert errors, f"{path.name} unexpectedly validated"
    else:
        assert not errors, f"{path.name}: {[e.message for e in errors]}"


@pytest.mark.parametrize("path", _fixtures("response"), ids=lambda p: p.stem)
def test_response_fixture_behaves_as_named(path: Path) -> None:
    data = json.loads(path.read_text())
    errors = list(_validator(_RESPONSE_SCHEMA).iter_errors(data))
    if "-invalid-" in path.name:
        assert errors, f"{path.name} unexpectedly validated"
    else:
        assert not errors, f"{path.name}: {[e.message for e in errors]}"


# ── additive-only Score change (ADR-0034) ─────────────────────────────────────


def _score_record(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "score_id": "01HXAY7M7QM4YZ2K7N9DPBYK2W",
        "subject": "sha256:" + "9f" * 32,
        "subject_kind": "span",
        "name": "answer_correct",
        "value": True,
        "value_type": "boolean",
        "source": "judge",
        "evaluator_id": "ci://x",
        "eval_card_digest": "sha256:" + "ab" * 32,
        "created_at": "2026-07-15T00:00:00+00:00",
    }
    base.update(over)
    return base


def test_score_without_supersedes_stays_valid() -> None:
    _validator(_SCORE_SCHEMA).validate(_score_record())


def test_score_with_supersedes_validates() -> None:
    _validator(_SCORE_SCHEMA).validate(
        _score_record(supersedes="01HXAY7M7QM4YZ2K7N9DPBYK2X")
    )
    _validator(_SCORE_SCHEMA).validate(_score_record(supersedes=None))


def test_score_with_bad_supersedes_rejected() -> None:
    errors = list(
        _validator(_SCORE_SCHEMA).iter_errors(_score_record(supersedes="not-a-ulid"))
    )
    assert errors
