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

"""Tests for the ADR-0117 ``ScoreConfig`` model, digest, and validation semantics.

Golden fixtures live in ``tests/fixtures/score-config/`` (4 valid + 7 invalid,
graduated from the accepted design draft). The valid fixtures carry **real**
``content_digest`` values, so they double as digest-determinism fixtures (C5).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.eval.score_config import (
    ScoreCategory,
    ScoreConfig,
    ScoreConfigViolation,
    ScoreDirection,
    ScoreRange,
    canonical_config_bytes,
    config_digest,
    validate_score_against_config,
)
from novafabric.eval.scores import Score, ScoreSource, ScoreValueType

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests" / "fixtures" / "score-config"
_SCHEMA = json.loads((_ROOT / "schemas" / "score-config-v0.schema.json").read_text())

_VALID = sorted(_FIXTURES.glob("valid-*.json"))
_INVALID = sorted(_FIXTURES.glob("invalid-*.json"))

_SUBJECT = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_CARD = "sha256:" + "60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"


def _score(**over: object) -> Score:
    base: dict[str, object] = {
        "subject": _SUBJECT,
        "name": "toxicity",
        "value": 0.5,
        "value_type": ScoreValueType.NUMERIC,
        "source": ScoreSource.CODE,
        "evaluator_id": "tox-scan",
        "eval_card_digest": _CARD,
    }
    base.update(over)
    return Score(**base)  # type: ignore[arg-type]


def _numeric_config(**over: object) -> ScoreConfig:
    base: dict[str, object] = {
        "name": "toxicity",
        "value_type": ScoreValueType.NUMERIC,
        "description": "Probability toxic; lower better.",
        "range": ScoreRange(min=0.0, max=1.0, direction=ScoreDirection.LOWER_BETTER),
    }
    base.update(over)
    return ScoreConfig(**base)  # type: ignore[arg-type]


def _categorical_config(**over: object) -> ScoreConfig:
    base: dict[str, object] = {
        "name": "helpfulness",
        "value_type": ScoreValueType.CATEGORICAL,
        "description": "How helpful the turn was.",
        "categories": [
            ScoreCategory(value="bad", ordinal=0),
            ScoreCategory(value="ok", ordinal=1),
            ScoreCategory(value="good", ordinal=2),
        ],
    }
    base.update(over)
    return ScoreConfig(**base)  # type: ignore[arg-type]


def _boolean_config(**over: object) -> ScoreConfig:
    base: dict[str, object] = {
        "name": "grounded",
        "value_type": ScoreValueType.BOOLEAN,
        "description": "True iff every claim is supported.",
    }
    base.update(over)
    return ScoreConfig(**base)  # type: ignore[arg-type]


# ── golden fixtures: JSON Schema conformance ─────────────────────────────────


def test_fixture_inventory() -> None:
    assert len(_VALID) == 4
    assert len(_INVALID) == 7


@pytest.mark.parametrize("path", _VALID, ids=lambda p: p.name)
def test_valid_fixtures_pass_json_schema(path: Path) -> None:
    jsonschema.validate(
        json.loads(path.read_text()),
        _SCHEMA,
        format_checker=jsonschema.FormatChecker(),
    )


@pytest.mark.parametrize("path", _INVALID, ids=lambda p: p.name)
def test_invalid_fixtures_fail_json_schema(path: Path) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            json.loads(path.read_text()),
            _SCHEMA,
            format_checker=jsonschema.FormatChecker(),
        )


# ── golden fixtures: Pydantic model agreement ────────────────────────────────


@pytest.mark.parametrize("path", _VALID, ids=lambda p: p.name)
def test_valid_fixtures_parse_and_digests_hold(path: Path) -> None:
    doc = json.loads(path.read_text())
    config = ScoreConfig.model_validate(doc)
    # C5: the stored digest equals the recomputed digest of the canonical body.
    assert config.content_digest == doc["content_digest"]
    assert config_digest(config) == doc["content_digest"]


@pytest.mark.parametrize("path", _INVALID, ids=lambda p: p.name)
def test_invalid_fixtures_rejected_by_model(path: Path) -> None:
    with pytest.raises(ValidationError):
        ScoreConfig.model_validate(json.loads(path.read_text()))


# ── digest derivation (C5) ───────────────────────────────────────────────────


def test_digest_auto_derived_on_construction() -> None:
    config = _boolean_config()
    assert config.content_digest.startswith("sha256:")
    assert config.content_digest == config_digest(config)


def test_equal_bodies_equal_digests() -> None:
    a = _numeric_config()
    b = _numeric_config()
    assert a.config_id != b.config_id  # envelope differs …
    assert a.content_digest == b.content_digest  # … definition body does not


def test_changed_body_changes_digest() -> None:
    a = _numeric_config()
    b = _numeric_config(description="Redefined.")
    assert a.content_digest != b.content_digest


def test_version_is_part_of_the_digested_body() -> None:
    assert _numeric_config().content_digest != _numeric_config(version=2).content_digest


def test_envelope_fields_do_not_enter_the_digest() -> None:
    payload = json.loads(canonical_config_bytes(_boolean_config()))
    assert set(payload) == {"description", "name", "value_type", "version"}


def test_mismatched_digest_rejected() -> None:
    doc = json.loads(_boolean_config().model_dump_json(exclude_none=True))
    doc["content_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValidationError, match="content_digest"):
        ScoreConfig.model_validate(doc)


# ── shape constraints beyond the JSON Schema ─────────────────────────────────


def test_range_min_must_not_exceed_max() -> None:
    with pytest.raises(ValidationError, match="min"):
        ScoreRange(min=1.0, max=0.0)


def test_range_degenerate_single_point_is_legal() -> None:
    assert ScoreRange(min=1.0, max=1.0).min == 1.0


def test_category_values_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="unique"):
        _categorical_config(
            categories=[ScoreCategory(value="ok"), ScoreCategory(value="ok")]
        )


def test_categorical_without_ordinals_is_nominal_and_legal() -> None:
    config = _categorical_config(
        categories=[ScoreCategory(value="a"), ScoreCategory(value="b")]
    )
    assert all(c.ordinal is None for c in config.categories or [])


def test_boolean_with_categories_rejected() -> None:
    with pytest.raises(ValidationError, match="boolean"):
        _boolean_config(categories=[ScoreCategory(value="yes")])


def test_categorical_requires_categories() -> None:
    with pytest.raises(ValidationError, match="categorical"):
        _categorical_config(categories=None)


def test_numeric_requires_range() -> None:
    with pytest.raises(ValidationError, match="numeric"):
        _numeric_config(range=None)


def test_numeric_with_categories_rejected() -> None:
    with pytest.raises(ValidationError, match="numeric"):
        _numeric_config(categories=[ScoreCategory(value="x")])


def test_bad_config_id_rejected() -> None:
    with pytest.raises(ValidationError, match="ULID"):
        _boolean_config(config_id="not-a-ulid")


def test_version_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _boolean_config(version=0)


# ── validation hook semantics (D2) ───────────────────────────────────────────


def test_numeric_score_within_range_passes() -> None:
    validate_score_against_config(_score(value=0.25), _numeric_config())


@pytest.mark.parametrize("bound", [0.0, 1.0])
def test_numeric_bounds_are_inclusive(bound: float) -> None:
    validate_score_against_config(_score(value=bound), _numeric_config())


def test_numeric_score_out_of_range_violates() -> None:
    with pytest.raises(ScoreConfigViolation, match="range"):
        validate_score_against_config(_score(value=1.5), _numeric_config())


def test_categorical_member_passes() -> None:
    score = _score(name="helpfulness", value="good", value_type=ScoreValueType.CATEGORICAL)
    validate_score_against_config(score, _categorical_config())


def test_categorical_typo_violates() -> None:
    score = _score(name="helpfulness", value="grate", value_type=ScoreValueType.CATEGORICAL)
    with pytest.raises(ScoreConfigViolation, match="grate"):
        validate_score_against_config(score, _categorical_config())


def test_boolean_score_passes() -> None:
    score = _score(name="grounded", value=True, value_type=ScoreValueType.BOOLEAN)
    validate_score_against_config(score, _boolean_config())


def test_value_type_disagreement_violates() -> None:
    score = _score(name="helpfulness", value=0.9)  # numeric under a categorical config
    with pytest.raises(ScoreConfigViolation, match="value_type"):
        validate_score_against_config(score, _categorical_config())


def test_name_mismatch_is_a_usage_error() -> None:
    with pytest.raises(ScoreConfigViolation, match="name"):
        validate_score_against_config(_score(name="other"), _numeric_config())
