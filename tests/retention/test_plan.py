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
"""Sweep planner tests: matching, due-ness, holds, conflicts (ADR-0134 D2/D4)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novafabric.retention.models import (
    Decision,
    RetentionAction,
    RetentionBinding,
    SweepItem,
)
from novafabric.retention.sweep import (
    HoldContext,
    enumerate_capsules,
    load_bindings,
    matches,
    plan_sweep,
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)
FRESH = NOW - timedelta(days=5)


def _binding(**overrides: object) -> RetentionBinding:
    data: dict[str, object] = {
        "id": "b1",
        "match": {"tag": "trade"},
        "window": "P90D",
        "action": "purge",
    }
    data.update(overrides)
    return RetentionBinding.model_validate(data)


def _item(**overrides: object) -> SweepItem:
    data: dict[str, object] = {
        "item_id": "cap-1",
        "created_at": OLD,
        "tags": {"trade"},
    }
    data.update(overrides)
    return SweepItem.model_validate(data)


# ---------------------------------------------------------------------------
# Matching by class of data
# ---------------------------------------------------------------------------


def test_match_by_tag_env_asset_and_age() -> None:
    item = _item(
        tags={"trade", "pii"},
        deployment_environment="production",
        asset_ids=["prompt:checkout"],
    )
    assert matches(_binding(match={"tag": "trade"}), item, NOW)
    assert matches(_binding(match={"tag": ["trade", "pii"]}), item, NOW)
    assert not matches(_binding(match={"tag": ["trade", "other"]}), item, NOW)
    assert matches(_binding(match={"deployment_environment": "production"}), item, NOW)
    assert matches(
        _binding(match={"deployment_environment": ["dev", "production"]}), item, NOW
    )
    assert not matches(_binding(match={"deployment_environment": "dev"}), item, NOW)
    assert matches(_binding(match={"asset": "prompt:*"}), item, NOW)
    assert not matches(_binding(match={"asset": "model:*"}), item, NOW)
    # age narrows which items are governed
    assert matches(_binding(match={"tag": "trade", "age": "P30D"}), item, NOW)
    assert not matches(
        _binding(match={"tag": "trade", "age": "P30D"}), _item(created_at=FRESH), NOW
    )
    # predicates are ANDed
    assert not matches(
        _binding(match={"tag": "trade", "deployment_environment": "dev"}), item, NOW
    )


def test_plan_selects_only_due_items_by_age_and_class() -> None:
    items = [
        _item(item_id="due-old", created_at=OLD),
        _item(item_id="not-due-fresh", created_at=FRESH),
        _item(item_id="wrong-class", created_at=OLD, tags={"other"}),
    ]
    plan = plan_sweep(items, [_binding()], HoldContext(), now=NOW)
    assert [d.item.item_id for d in plan] == ["due-old"]
    assert plan[0].decision is Decision.DUE
    assert plan[0].action is RetentionAction.PURGE
    assert plan[0].due_at == OLD + timedelta(days=90)


def test_absolute_date_window() -> None:
    past = _binding(id="cutoff", window="2026-01-01")
    future = _binding(id="later", window="2031-01-01")
    plan = plan_sweep([_item()], [past], HoldContext(), now=NOW)
    assert len(plan) == 1 and plan[0].decision is Decision.DUE
    assert plan_sweep([_item()], [future], HoldContext(), now=NOW) == []


def test_no_bindings_sweeps_nothing() -> None:
    assert plan_sweep([_item()], [], HoldContext(), now=NOW) == []


def test_disabled_binding_is_never_swept() -> None:
    assert plan_sweep([_item()], [_binding(enabled=False)], HoldContext(), now=NOW) == []


# ---------------------------------------------------------------------------
# D4 — WORM/legal holds always win
# ---------------------------------------------------------------------------


def test_worm_locked_item_is_never_acted_on_before_retention_date() -> None:
    """A WORM-retained capsule is NEVER purged (or shredded) before locked_until."""
    holds = HoldContext(worm_locks={"cap-1": NOW + timedelta(days=365)})
    for action in ("purge", "crypto-shred", "expire-metadata"):
        plan = plan_sweep([_item()], [_binding(action=action)], holds, now=NOW)
        assert len(plan) == 1
        assert plan[0].decision is Decision.HELD
        assert plan[0].reason == "worm_hold"
        assert plan[0].worm_locked_until == NOW + timedelta(days=365)


def test_expired_worm_lock_no_longer_blocks() -> None:
    holds = HoldContext(worm_locks={"cap-1": NOW - timedelta(days=1)})
    plan = plan_sweep([_item()], [_binding()], holds, now=NOW)
    assert plan[0].decision is Decision.DUE


def test_active_legal_hold_blocks_every_action() -> None:
    holds = HoldContext(active_hold_ids=["litigation-42"])
    for action in ("purge", "crypto-shred", "expire-metadata"):
        plan = plan_sweep([_item()], [_binding(action=action)], holds, now=NOW)
        assert plan[0].decision is Decision.HELD
        assert plan[0].reason == "legal_hold_active"


def test_binding_declared_legal_hold_is_inert() -> None:
    plan = plan_sweep([_item()], [_binding(legal_hold=True)], HoldContext(), now=NOW)
    assert plan[0].decision is Decision.HELD
    assert plan[0].reason == "legal_hold_active"


def test_purge_refused_when_deletion_prohibited() -> None:
    holds = HoldContext(deletion_mode="prohibited")
    plan = plan_sweep([_item()], [_binding(action="purge")], holds, now=NOW)
    assert plan[0].decision is Decision.HELD
    assert plan[0].reason == "deletion_prohibited"
    # crypto-shred is still allowed: it preserves the object (D4)
    plan = plan_sweep([_item()], [_binding(action="crypto-shred")], holds, now=NOW)
    assert plan[0].decision is Decision.DUE


# ---------------------------------------------------------------------------
# Conflict resolution (spec "Edge cases")
# ---------------------------------------------------------------------------


def test_purge_plus_shred_conflict_is_ambiguous_binding_error() -> None:
    bindings = [
        _binding(id="b-purge", action="purge"),
        _binding(id="b-shred", action="crypto-shred"),
    ]
    plan = plan_sweep([_item()], bindings, HoldContext(), now=NOW)
    assert len(plan) == 1
    assert plan[0].decision is Decision.ERROR
    assert plan[0].reason == "ambiguous_binding"
    assert set(plan[0].matched_binding_ids) == {"b-purge", "b-shred"}


def test_least_destructive_action_wins_and_records_all_bindings() -> None:
    bindings = [
        _binding(id="b-purge", action="purge"),
        _binding(id="b-expire", action="expire-metadata"),
    ]
    plan = plan_sweep([_item()], bindings, HoldContext(), now=NOW)
    assert len(plan) == 1
    assert plan[0].action is RetentionAction.EXPIRE_METADATA
    assert plan[0].binding_id == "b-expire"
    assert set(plan[0].matched_binding_ids) == {"b-purge", "b-expire"}
    assert plan[0].decision is Decision.DUE


def test_already_expired_item_is_terminal_noop() -> None:
    plan = plan_sweep(
        [_item(expired=True)],
        [_binding(action="expire-metadata")],
        HoldContext(),
        now=NOW,
    )
    assert plan == []


# ---------------------------------------------------------------------------
# Policy-file loading and capsule enumeration
# ---------------------------------------------------------------------------


def test_load_bindings_missing_file_and_missing_block(tmp_path: Path) -> None:
    assert load_bindings(tmp_path / "absent.yaml") == []
    p = tmp_path / "retention-policy.yaml"
    p.write_text("version: 1\nregistry: r\nretention_days: 30\n")
    assert load_bindings(p) == []


def test_load_bindings_parses_additive_block(tmp_path: Path) -> None:
    p = tmp_path / "retention-policy.yaml"
    p.write_text(
        """
version: 1
registry: r
retention_days: 1825
deletion_mode: defensible
bindings:
  - id: mifid-trade-5y
    match:
      tag: trade-record
    window: P1825D
    action: purge
  - id: gdpr-subject-pii
    match:
      tag: contains-pii
    window: P180D
    action: crypto-shred
"""
    )
    bindings = load_bindings(p)
    assert [b.id for b in bindings] == ["mifid-trade-5y", "gdpr-subject-pii"]
    assert bindings[1].action is RetentionAction.CRYPTO_SHRED
    # ADR-0031 loader still reads the same file (additive, no break)
    from novafabric.storage._retention import RetentionPolicy

    assert RetentionPolicy.from_yaml(p).retention_days == 1825


def test_load_bindings_rejects_malformed_and_duplicate(tmp_path: Path) -> None:
    import pytest

    p = tmp_path / "retention-policy.yaml"
    p.write_text("bindings:\n  - id: b\n    match: {}\n    window: P1D\n    action: purge\n")
    with pytest.raises(ValueError, match="malformed"):
        load_bindings(p)
    p.write_text(
        "bindings:\n"
        "  - {id: b, match: {tag: x}, window: P1D, action: purge}\n"
        "  - {id: b, match: {tag: y}, window: P2D, action: purge}\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_bindings(p)


def _write_capsule(
    capsule_dir: Path,
    run_id: str,
    created_at: datetime,
    metadata: dict[str, str] | None = None,
    alias: str | None = None,
) -> Path:
    d = capsule_dir / run_id
    d.mkdir(parents=True)
    manifest: dict[str, object] = {
        "run_id": run_id,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
    }
    if metadata:
        manifest["metadata"] = metadata
    if alias:
        manifest["alias"] = alias
    (d / "capsule.json").write_text(json.dumps(manifest))
    return d


def test_enumerate_capsules_reads_matching_attributes(tmp_path: Path) -> None:
    _write_capsule(
        tmp_path,
        "cap-a",
        OLD,
        metadata={
            "tags": "trade, pii",
            "deployment_environment": "production",
            "pii_subject_id": "user@example.com",
        },
        alias="prompt:checkout",
    )
    (tmp_path / "cap-a" / "retention-expired.json").write_text("{}")
    _write_capsule(tmp_path, "cap-b", FRESH)
    # unreadable manifest is skipped, never deleted by accident
    bad = tmp_path / "cap-bad"
    bad.mkdir()
    (bad / "capsule.json").write_text("{not json")

    items = {i.item_id: i for i in enumerate_capsules(tmp_path)}
    assert set(items) == {"cap-a", "cap-b"}
    a = items["cap-a"]
    assert a.tags == {"trade", "pii"}
    assert a.deployment_environment == "production"
    assert a.pii_subject_id == "user@example.com"
    assert a.asset_ids == ["prompt:checkout"]
    assert a.expired is True
    assert a.created_at == OLD
    assert items["cap-b"].tags == set()
    assert items["cap-b"].expired is False


def test_enumerate_capsules_missing_dir_is_empty(tmp_path: Path) -> None:
    assert enumerate_capsules(tmp_path / "nope") == []
