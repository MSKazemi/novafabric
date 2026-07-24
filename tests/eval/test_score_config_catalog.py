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

"""Tests for the ADR-0117 score-configuration catalog (SQLite) and append hook.

All tests use an isolated temp SQLite DB (``db_path`` fixture) — never the real
registry. Immutability (I1/C4), identical-body no-op (C5), resolution rules, and
the opt-in validated append path are covered here; the record model itself is
covered in ``test_score_config.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.eval.score_config import (
    ScoreCategory,
    ScoreConfig,
    ScoreConfigViolation,
    ScoreRange,
)
from novafabric.eval.score_config_catalog import (
    ScoreConfigImmutabilityError,
    ScoreConfigNotFoundError,
    append_score_validated,
    find_config_for_score,
    get_config,
    get_config_by_digest,
    list_configs,
    register_config,
    register_config_record,
    resolve_config_ref,
)
from novafabric.eval.scores import Score, ScoreSource, ScoreValueType, read_scores

_SUBJECT = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_CARD = "sha256:" + "60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


def _register_toxicity(db_path: Path, description: str = "Lower is better.") -> ScoreConfig:
    return register_config(
        name="toxicity",
        value_type=ScoreValueType.NUMERIC,
        description=description,
        range_=ScoreRange(min=0.0, max=1.0),
        db_path=db_path,
    )


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


# ── register / version lifecycle (I1, C5) ────────────────────────────────────


def test_first_registration_is_version_1(db_path: Path) -> None:
    config = _register_toxicity(db_path)
    assert config.version == 1
    assert config.content_digest.startswith("sha256:")


def test_identical_body_reregistration_is_a_noop(db_path: Path) -> None:
    first = _register_toxicity(db_path)
    again = _register_toxicity(db_path)
    assert again.config_id == first.config_id
    assert again.version == 1
    assert again.content_digest == first.content_digest
    assert len(list_configs(all_versions=True, db_path=db_path)) == 1


def test_changed_body_bumps_version_never_edits(db_path: Path) -> None:
    v1 = _register_toxicity(db_path)
    v2 = _register_toxicity(db_path, description="Redefined metric.")
    assert v2.version == 2
    assert v2.content_digest != v1.content_digest
    # v1 is still resolvable, untouched (immutability).
    assert get_config("toxicity", version=1, db_path=db_path).content_digest == v1.content_digest


def test_register_config_record_conflicting_body_refused(db_path: Path) -> None:
    _register_toxicity(db_path)
    imposter = ScoreConfig(
        name="toxicity",
        value_type=ScoreValueType.NUMERIC,
        description="A different definition at the same version.",
        range=ScoreRange(min=0.0, max=10.0),
        version=1,
    )
    with pytest.raises(ScoreConfigImmutabilityError):
        register_config_record(imposter, db_path=db_path)


def test_register_config_record_identical_body_is_a_noop(db_path: Path) -> None:
    first = _register_toxicity(db_path)
    stored = register_config_record(first, db_path=db_path)
    assert stored.config_id == first.config_id
    assert len(list_configs(all_versions=True, db_path=db_path)) == 1


def test_register_config_record_new_name_and_version(db_path: Path) -> None:
    record = ScoreConfig(
        name="grounded",
        value_type=ScoreValueType.BOOLEAN,
        description="Imported from another machine.",
        version=3,
    )
    stored = register_config_record(record, db_path=db_path)
    assert stored.version == 3
    assert get_config("grounded", db_path=db_path).version == 3


# ── resolution ───────────────────────────────────────────────────────────────


def test_get_latest_by_bare_name(db_path: Path) -> None:
    _register_toxicity(db_path)
    v2 = _register_toxicity(db_path, description="v2")
    assert get_config("toxicity", db_path=db_path).content_digest == v2.content_digest


def test_get_by_digest(db_path: Path) -> None:
    config = _register_toxicity(db_path)
    got = get_config_by_digest(config.content_digest, db_path=db_path)
    assert got.name == "toxicity"
    assert got.content_digest == config.content_digest


def test_resolve_ref_forms(db_path: Path) -> None:
    v1 = _register_toxicity(db_path)
    v2 = _register_toxicity(db_path, description="v2")
    assert resolve_config_ref("toxicity", db_path=db_path).version == 2
    assert resolve_config_ref("toxicity@1", db_path=db_path).content_digest == v1.content_digest
    assert resolve_config_ref(v2.content_digest, db_path=db_path).version == 2


def test_resolve_ref_bad_version_suffix(db_path: Path) -> None:
    with pytest.raises(ValueError, match="version"):
        resolve_config_ref("toxicity@latest", db_path=db_path)


def test_missing_config_raises(db_path: Path) -> None:
    with pytest.raises(ScoreConfigNotFoundError):
        get_config("nope", db_path=db_path)
    with pytest.raises(ScoreConfigNotFoundError):
        get_config("nope", version=1, db_path=db_path)
    with pytest.raises(ScoreConfigNotFoundError):
        get_config_by_digest("sha256:" + "0" * 64, db_path=db_path)


def test_list_latest_per_name_vs_all(db_path: Path) -> None:
    _register_toxicity(db_path)
    _register_toxicity(db_path, description="v2")
    register_config(
        name="helpfulness",
        value_type=ScoreValueType.CATEGORICAL,
        description="Ordinal helpfulness.",
        categories=[ScoreCategory(value="bad", ordinal=0), ScoreCategory(value="good", ordinal=1)],
        db_path=db_path,
    )
    latest = list_configs(db_path=db_path)
    assert {(c.name, c.version) for c in latest} == {("toxicity", 2), ("helpfulness", 1)}
    assert len(list_configs(all_versions=True, db_path=db_path)) == 3


# ── validated append hook (D2) ───────────────────────────────────────────────


def test_free_score_without_config_appends_unchanged(db_path: Path, tmp_path: Path) -> None:
    scores_file = tmp_path / "scores.jsonl"
    used = append_score_validated(scores_file, _score(name="unconfigured"), db_path=db_path)
    assert used is None
    assert len(read_scores(scores_file)) == 1


def test_matching_config_valid_score_appends_and_pins(db_path: Path, tmp_path: Path) -> None:
    config = _register_toxicity(db_path)
    scores_file = tmp_path / "scores.jsonl"
    used = append_score_validated(scores_file, _score(value=0.2), db_path=db_path)
    assert used is not None
    assert used.content_digest == config.content_digest
    assert len(read_scores(scores_file)) == 1


def test_violation_raises_and_nothing_is_appended(db_path: Path, tmp_path: Path) -> None:
    _register_toxicity(db_path)
    scores_file = tmp_path / "scores.jsonl"
    with pytest.raises(ScoreConfigViolation):
        append_score_validated(scores_file, _score(value=2.0), db_path=db_path)
    assert read_scores(scores_file) == []
    assert not scores_file.exists()


def test_find_config_for_score(db_path: Path) -> None:
    assert find_config_for_score("toxicity", db_path=db_path) is None
    _register_toxicity(db_path)
    found = find_config_for_score("toxicity", db_path=db_path)
    assert found is not None and found.name == "toxicity"
