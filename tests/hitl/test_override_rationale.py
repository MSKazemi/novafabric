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

"""ADR-0150 P2 — override (NF-187) and rationale (NF-188) records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.hitl import (
    IdentityRefError,
    NoOpOverrideError,
    OverrideRecord,
    RationaleRecord,
    RecordContentError,
    digest_turn,
    load_overrides,
    load_rationales,
    record_override,
    record_rationale,
)

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "conversation"
THREADED = FIXTURES / "threaded-capsule.json"
ACCOUNTABILITY = FIXTURES / "accountability-capsule.json"


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _override(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "turn_ref": "t2",
        "overridden_action_digest": digest_turn("rollback --all"),
        "override_action_digest": digest_turn("rollback --canary"),
        "overrider": "human:fp:9f2c4a1b7e0d5638",
        "reason": "scope_too_broad",
        "at": "2026-07-15T10:00:31Z",
    }
    base.update(kw)
    return base


def _rationale(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "turn_ref": "t1",
        "stated_reason_digest": digest_turn("rollback reduces blast radius"),
        "surfaced_at": "2026-07-15T10:00:03Z",
        "model_ref": "acme/triager-v3",
    }
    base.update(kw)
    return base


# ── Override (NF-187) ─────────────────────────────────────────────────────


def test_golden_capsule_override_loads() -> None:
    loaded = load_overrides(_load(ACCOUNTABILITY))
    assert not loaded.defects
    assert [r.turn_ref for _, r in loaded.records] == ["t2"]


def test_override_must_change_the_action() -> None:
    same = digest_turn("rollback --all")
    with pytest.raises(NoOpOverrideError):
        OverrideRecord.model_validate(
            _override(overridden_action_digest=same, override_action_digest=same)
        )


@pytest.mark.parametrize(
    ("field", "value", "exc"),
    [
        ("overrider", "agent:spiffe://x/sa/bot", IdentityRefError),
        ("overrider", "human:bob@example.com", IdentityRefError),
        ("override_action_digest", "rollback --canary", RecordContentError),
        ("overridden_action_digest", b"raw", RecordContentError),
        ("reason", "Bob said it was too broad", RecordContentError),
        ("at", "not a time", Exception),
    ],
)
def test_override_field_invariants(field: str, value: Any, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        OverrideRecord.model_validate(_override(**{field: value}))


def test_trailing_newline_digest_cannot_bypass_no_op_check() -> None:
    """``$`` matches before a final ``\\n``; the anchors must be end-of-string."""
    same = digest_turn("rollback --all")
    with pytest.raises(RecordContentError):
        OverrideRecord.model_validate(
            _override(overridden_action_digest=same, override_action_digest=same + "\n")
        )


@pytest.mark.parametrize("reason", ["approve\n", "approve\r", " approve"])
def test_reason_code_with_trailing_newline_is_rejected(reason: str) -> None:
    with pytest.raises(RecordContentError):
        OverrideRecord.model_validate(_override(reason=reason))


def test_reason_digest_with_trailing_newline_is_rejected() -> None:
    with pytest.raises(RecordContentError):
        OverrideRecord.model_validate(_override(reason=digest_turn("prose") + "\n"))


def test_several_overrides_may_share_a_turn() -> None:
    first = record_override(_load(THREADED), _override())
    second = record_override(first.capsule, _override(override_action_digest=digest_turn("pause")))
    assert second.recorded
    assert len(load_overrides(second.capsule).records) == 2


def test_override_record_fails_open() -> None:
    cap = _load(THREADED)
    outcome = record_override(cap, _override(turn_ref="t9"))
    assert not outcome.recorded and outcome.capsule is cap
    outcome = record_override(cap, _override(reason="free text"))
    assert outcome.reason == "RecordContentError"
    assert record_override(cap, OverrideRecord.model_validate(_override())).recorded


def test_malformed_stored_override_is_a_defect() -> None:
    cap = _load(ACCOUNTABILITY)
    cap["facets"]["conversation"]["override"].append({"turn_ref": "t2"})
    loaded = load_overrides(cap)
    assert len(loaded.records) == 1
    assert loaded.defects[0].index == 1


# ── Rationale (NF-188) ────────────────────────────────────────────────────


def test_golden_capsule_rationale_loads() -> None:
    loaded = load_rationales(_load(ACCOUNTABILITY))
    assert [r.model_ref for _, r in loaded.records] == ["acme/triager-v3"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stated_reason_digest", "rollback reduces blast radius"),
        ("model_ref", "the triage model"),
        ("model_ref", 42),
        ("model_ref", "acme/triager-v3\n"),
        ("stated_reason_digest", digest_turn("x") + "\n"),
        ("transcript", "x"),
    ],
)
def test_rationale_is_digest_only(field: str, value: Any) -> None:
    with pytest.raises(RecordContentError):
        RationaleRecord.model_validate(_rationale(**{field: value}))


def test_rationale_record_and_fail_open() -> None:
    cap = _load(THREADED)
    assert record_rationale(cap, _rationale()).recorded
    assert record_rationale(cap, RationaleRecord.model_validate(_rationale())).recorded
    outcome = record_rationale(cap, _rationale(turn_ref="nope"))
    assert not outcome.recorded and outcome.capsule is cap


def test_no_records_means_nothing_loaded() -> None:
    assert load_rationales(_load(THREADED)).records == []
    assert load_overrides({"facets": "x"}).records == []
