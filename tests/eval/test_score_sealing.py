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

"""NF-002 requirement 10 — a Score written into a capsule is sealed.

Proves at integration level that ``scores.jsonl`` is covered by the capsule Merkle
root (``capsule_merkle_root`` — the Evidence-Bundle subject digest), so score tampering
is detected at Evidence-Bundle creation. No change to the seal path is required: the
Merkle root already hashes every file in the capsule directory.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.eval.card import Calibration, EvalCard, JudgeModel, card_digest, sign_card
from novafabric.eval.scores import (
    SCORES_FILENAME,
    Score,
    ScoreSource,
    ScoreValueType,
    append_score,
)
from novafabric.evidence.merkle import capsule_merkle_root

_SUBJECT = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "trace.jsonl").write_text('{"event": "start"}\n')
    return cap


def _digest_of_a_card() -> str:
    card: EvalCard = sign_card(
        EvalCard(
            card_id="faithfulness-judge",
            name="J",
            version="1.0.0",
            source=ScoreSource.JUDGE,
            judge_model=JudgeModel(name="m", endpoint_ref="env:NOVA_JUDGE_ENDPOINT"),
            prompt_version="sha256:aa",
            rubric="r",
            dataset_version="d@1",
            calibration=Calibration(human_agreement=0.8, n=50, metric="cohen_kappa"),
        ),
        Ed25519PrivateKey.generate(),
        "k",
    )
    return card_digest(card)


def _score(value: float) -> Score:
    return Score(
        subject=_SUBJECT,
        name="faithfulness",
        value=value,
        value_type=ScoreValueType.NUMERIC,
        source=ScoreSource.JUDGE,
        evaluator_id="faithfulness-judge",
        eval_card_digest=_digest_of_a_card(),
    )


def test_adding_a_score_changes_the_capsule_root(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    root_before = capsule_merkle_root(cap)
    assert root_before.startswith("sha256:")

    append_score(cap / SCORES_FILENAME, _score(0.82))
    root_with_score = capsule_merkle_root(cap)
    # The sealed subject digest now covers the score — it is part of the evidence.
    assert root_with_score != root_before


def test_tampering_with_a_score_is_detected(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    scores_path = cap / SCORES_FILENAME
    append_score(scores_path, _score(0.82))
    sealed_root = capsule_merkle_root(cap)

    # Tamper: rewrite the score log with a different value.
    scores_path.write_text(_score(0.99).model_dump_json(exclude_none=True) + "\n")
    tampered_root = capsule_merkle_root(cap)

    assert tampered_root != sealed_root  # req 10: tampering breaks the subject digest


def test_capsule_without_scores_is_still_valid(tmp_path: Path) -> None:
    # Backward-compat: a pre-NF-010 capsule (no scores.jsonl) still seals fine.
    cap = _capsule(tmp_path)
    root = capsule_merkle_root(cap)
    assert root.startswith("sha256:")
    assert not (cap / SCORES_FILENAME).exists()
