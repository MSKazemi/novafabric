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

"""Tests for the NF-002 content-addressed eval-card registry (ADR-0099).

All tests use an isolated temp SQLite DB (``db_path`` fixture) — never the real
registry database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.eval.card import Calibration, EvalCard, JudgeModel, card_digest, sign_card
from novafabric.eval.registry import (
    DuplicateEvalCardError,
    EvalCardNotFoundError,
    UnsignedCardError,
    asset_ref,
    card_exists,
    get_card,
    get_card_by_digest,
    list_cards,
    register_card,
)
from novafabric.eval.scores import ScoreSource


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


def _signed_judge(**over: object) -> EvalCard:
    base: dict[str, object] = {
        "card_id": "faithfulness-judge",
        "name": "Answer Faithfulness Judge",
        "version": "1.2.0",
        "source": ScoreSource.JUDGE,
        "judge_model": JudgeModel(name="self-hosted/llama-3.3-70b", endpoint_ref="env:NOVA_JUDGE_ENDPOINT"),
        "prompt_version": "sha256:aa11",
        "rubric": "grounded?",
        "dataset_version": "golden@3.1.0",
        "calibration": Calibration(human_agreement=0.86, n=120, metric="cohen_kappa"),
    }
    base.update(over)
    card = EvalCard(**base)  # type: ignore[arg-type]
    return sign_card(card, Ed25519PrivateKey.generate(), key_id="nf-signer-1")


def _signed_code(**over: object) -> EvalCard:
    base: dict[str, object] = {
        "card_id": "exact-match", "name": "Exact Match", "version": "0.1.0", "source": ScoreSource.CODE
    }
    base.update(over)
    return sign_card(EvalCard(**base), Ed25519PrivateKey.generate(), "k")  # type: ignore[arg-type]


def test_register_and_resolve(db_path: Path) -> None:
    card = _signed_judge()
    digest = register_card(card, db_path=db_path)
    assert digest == card_digest(card)
    assert card_exists(digest, db_path=db_path)
    assert get_card_by_digest(digest, db_path=db_path).card_id == "faithfulness-judge"
    assert get_card("faithfulness-judge", "1.2.0", db_path=db_path).version == "1.2.0"


def test_asset_ref_form() -> None:
    ref = asset_ref(_signed_judge())
    assert ref.startswith("eval-card:faithfulness-judge@1.2.0+sha256:")


def test_duplicate_version_rejected(db_path: Path) -> None:
    register_card(_signed_judge(), db_path=db_path)
    with pytest.raises(DuplicateEvalCardError):
        register_card(_signed_judge(), db_path=db_path)


def test_unsigned_card_refused(db_path: Path) -> None:
    unsigned = EvalCard(
        card_id="x", name="x", version="0.1.0", source=ScoreSource.CODE
    )
    with pytest.raises(UnsignedCardError):
        register_card(unsigned, db_path=db_path)


def test_card_exists_false_for_unknown(db_path: Path) -> None:
    assert not card_exists("sha256:" + "0" * 64, db_path=db_path)


def test_get_missing_by_digest_raises(db_path: Path) -> None:
    with pytest.raises(EvalCardNotFoundError):
        get_card_by_digest("sha256:" + "0" * 64, db_path=db_path)


def test_get_missing_by_ref_raises(db_path: Path) -> None:
    with pytest.raises(EvalCardNotFoundError):
        get_card("nope", "9.9.9", db_path=db_path)


def test_list_and_filter(db_path: Path) -> None:
    register_card(_signed_judge(), db_path=db_path)
    register_card(_signed_code(), db_path=db_path)
    assert len(list_cards(db_path=db_path)) == 2
    judges = list_cards(source="judge", db_path=db_path)
    assert len(judges) == 1
    assert judges[0].source is ScoreSource.JUDGE
