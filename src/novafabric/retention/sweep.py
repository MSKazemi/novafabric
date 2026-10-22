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
"""Retention sweep planner (ADR-0134 D2/D3/D4): pure due-ness + hold logic.

The planner is a stateless function over ground truth: items, bindings, and
the hold context. The dry-run preview (``nova retention plan``) and the real
run (``nova retention apply``) share this exact code path (ADR-0134 D3).

Safety invariant (D4, fixed, not configurable): a WORM/legal hold always
wins. No action — not even ``crypto-shred`` — is applied to an item whose
WORM ``locked_until`` is in the future or whose registry has an active legal
hold. Such items are recorded ``skipped`` and never silently dropped.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping

import yaml
from pydantic import BaseModel, Field, ValidationError

from novafabric._capsule_fs import (
    CapsuleEntryError,
    read_capsule_member_or_none,
    read_capsule_text,
)

from novafabric.retention.models import (
    ACTION_SEVERITY,
    Decision,
    ManifestName,
    PlannedDecision,
    RetentionAction,
    RetentionBinding,
    SweepItem,
    SweepOptions,
)
from novafabric.retention.windows import compute_due_at

log = logging.getLogger(__name__)

#: Marker file written into a capsule directory by the ``expire-metadata`` action.
EXPIRED_MARKER = "retention-expired.json"
#: Legacy manifest (``CapsuleWriter``); always acted on by ``apply``.
MANIFEST_JSON: ManifestName = "capsule.json"
#: Manifest ``nova capture`` and the adapters write; acted on only on opt-in.
MANIFEST_YAML: ManifestName = "capsule.yaml"
#: ADR-0314 per-capsule PII bookkeeping (``nova capture --pii-subject``).
REDACTION_MANIFEST = "redaction_manifest.json"
#: Upper bound on any one capsule file the sweep reads (manifest, bookkeeping).
MAX_SWEEP_FILE_BYTES = 32 * 1024 * 1024


class HoldContext(BaseModel):
    """Registry-level hold state consulted before any action (ADR-0031/D4)."""

    active_hold_ids: list[str] = Field(default_factory=list)
    deletion_mode: Literal["defensible", "prohibited"] = "defensible"
    worm_locks: Mapping[str, datetime] = Field(default_factory=dict)


def load_bindings(policy_path: Path) -> list[RetentionBinding]:
    """Load the optional ``bindings:`` block from an ADR-0031 ``retention-policy.yaml``.

    A missing file or absent ``bindings:`` block yields ``[]`` — a registry
    with no bindings is swept for nothing (ADR-0134 D8).

    Raises:
        ValueError: if the file exists but a binding is malformed.
    """
    if not policy_path.exists():
        return []
    data = yaml.safe_load(policy_path.read_text())
    if not isinstance(data, dict):
        return []
    raw = data.get("bindings") or []
    try:
        bindings = [RetentionBinding.model_validate(b) for b in raw]
    except ValidationError as exc:
        raise ValueError(f"malformed retention binding in {policy_path}: {exc}") from exc
    seen: set[str] = set()
    for b in bindings:
        if b.id in seen:
            raise ValueError(f"duplicate binding id {b.id!r} in {policy_path}")
        seen.add(b.id)
    return bindings


def load_sweep_options(policy_path: Path) -> SweepOptions:
    """Load the optional top-level sweep switches from ``retention-policy.yaml``.

    A missing file or absent key yields the defaults (``include_capture_capsules:
    false``).

    Raises:
        ValueError: if the file sets a switch to a value of the wrong type.
    """
    if not policy_path.exists():
        return SweepOptions()
    data = yaml.safe_load(policy_path.read_text())
    if not isinstance(data, dict):
        return SweepOptions()
    try:
        return SweepOptions.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"malformed retention sweep option in {policy_path}: {exc}") from exc


def _parse_created_at(raw: object) -> datetime:
    """``created_at`` as an aware UTC datetime (YAML may already have parsed it)."""
    if isinstance(raw, datetime):
        created = raw
    elif isinstance(raw, str):
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        raise TypeError(f"created_at is {type(raw).__name__}, not a timestamp")
    return created if created.tzinfo is not None else created.replace(tzinfo=timezone.utc)


def _read_manifest(manifest_path: Path) -> dict[str, object]:
    # No link, FIFO or device is followed, and the read is bounded (R6 helpers).
    text = read_capsule_text(
        manifest_path.parent,
        manifest_path.name,
        operation="a retention sweep",
        max_bytes=MAX_SWEEP_FILE_BYTES,
    )
    data = json.loads(text) if manifest_path.name == MANIFEST_JSON else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise TypeError("manifest is not a mapping")
    return data


def _envelope_subject_refs(capsule_dir: Path) -> list[str]:
    """Subject HMACs whose PII ``nova capture --pii-subject`` encrypted in this capsule.

    Read from ``redaction_manifest.json`` entries with ``protection: envelope``
    (ADR-0314 D5). The raw subject id is never in the capsule; it is resolved
    against ``dek.db`` with ``NOVA_PII_PEPPER`` at apply time. Unreadable or
    absent bookkeeping yields ``[]``.
    """
    path = capsule_dir / REDACTION_MANIFEST
    text = read_capsule_member_or_none(
        path, operation="a retention sweep", max_bytes=MAX_SWEEP_FILE_BYTES
    )
    if text is None:
        return []
    try:
        entries = json.loads(text).get("entries") or []
    except (ValueError, AttributeError) as exc:
        log.warning("retention: unreadable %s: %s", path, exc)
        return []
    refs = {
        str(e["subject_id_hmac"])
        for e in entries
        if isinstance(e, dict) and e.get("protection") == "envelope" and e.get("subject_id_hmac")
    }
    return sorted(refs)


def _manifest_paths(capsule_dir: Path) -> list[Path]:
    """One manifest per capsule directory; ``capsule.json`` wins when both exist.

    Preferring ``capsule.json`` keeps a directory that has both manifests swept
    exactly as before ``capsule.yaml`` was enumerated.
    """
    found: dict[Path, Path] = {}
    for name in (MANIFEST_YAML, MANIFEST_JSON):  # JSON last, so it overrides
        for manifest_path in capsule_dir.glob(f"*/{name}"):
            if manifest_path.parent.is_symlink():
                # A linked run directory is not a capsule this sweep owns: never
                # expire, purge or shred through a link.
                log.warning("retention: skipping linked capsule dir %s", manifest_path.parent)
                continue
            found[manifest_path.parent] = manifest_path
    return [found[d] for d in sorted(found)]


def enumerate_capsules(capsule_dir: Path) -> list[SweepItem]:
    """Enumerate sweepable capsules: ``<capsule_dir>/<run_id>/capsule.{json,yaml}``.

    Matching attributes are read from the capsule manifest:
    ``deployment_environment`` (``metadata.deployment_environment`` in
    ``capsule.json``; top-level, as ``nova capture`` writes it, in
    ``capsule.yaml``), ``metadata.tags`` (comma-separated),
    ``metadata.pii_subject_id`` (crypto-shred subject), the envelope subject
    refs in ``redaction_manifest.json``, and ``alias`` (matched by the ``asset``
    predicate). Each item records which manifest it came from: whether ``apply``
    may act on a ``capsule.yaml`` capsule is decided by the policy
    (:func:`split_by_scope`). Unreadable manifests are skipped with a warning —
    a broken capsule is never deleted by accident.
    """
    items: list[SweepItem] = []
    if not capsule_dir.is_dir():
        return items
    for manifest_path in _manifest_paths(capsule_dir):
        try:
            data = _read_manifest(manifest_path)
            created_at = _parse_created_at(data["created_at"])
        except (
            OSError, ValueError, KeyError, TypeError, yaml.YAMLError, CapsuleEntryError
        ) as exc:
            log.warning("retention: skipping unreadable capsule %s: %s", manifest_path, exc)
            continue
        manifest: ManifestName = (
            MANIFEST_JSON if manifest_path.name == MANIFEST_JSON else MANIFEST_YAML
        )
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        env = metadata.get("deployment_environment")
        if manifest == MANIFEST_YAML and env is None:
            env = data.get("deployment_environment")
        tags_raw = str(metadata.get("tags", ""))
        tags = {t.strip() for t in tags_raw.split(",") if t.strip()}
        alias = data.get("alias")
        item_dir = manifest_path.parent
        subject = metadata.get("pii_subject_id")
        items.append(
            SweepItem(
                item_id=str(data.get("run_id", item_dir.name)),
                created_at=created_at,
                path=item_dir,
                manifest=manifest,
                deployment_environment=str(env) if env is not None else None,
                tags=tags,
                asset_ids=[str(alias)] if alias else [],
                pii_subject_id=str(subject) if subject is not None else None,
                pii_subject_refs=_envelope_subject_refs(item_dir),
                expired=(item_dir / EXPIRED_MARKER).exists(),
            )
        )
    return items


def split_by_scope(
    plan: list[PlannedDecision], options: SweepOptions
) -> tuple[list[PlannedDecision], list[PlannedDecision]]:
    """Split *plan* into ``(acted_on, excluded)`` by the policy's sweep options.

    Without ``include_capture_capsules: true`` every ``capsule.yaml`` decision is
    excluded: ``apply`` never acts on it and records nothing for it, while
    ``plan``/``status`` still report it. Holds were already resolved by
    :func:`plan_sweep` identically for both manifests.
    """
    if options.include_capture_capsules:
        return list(plan), []
    acted = [d for d in plan if d.item.manifest != MANIFEST_YAML]
    excluded = [d for d in plan if d.item.manifest == MANIFEST_YAML]
    return acted, excluded


def capture_exclusion_notice(
    enumerated: int, excluded_decisions: int, policy_path: Path
) -> str | None:
    """Operator notice for capture capsules ``apply`` will not act on, or ``None``."""
    if enumerated == 0:
        return None
    return (
        f"{enumerated} capture capsule(s) (capsule.yaml, written by `nova capture` and "
        f"the framework adapters) are excluded from `nova retention apply`; "
        f"{excluded_decisions} of them match a binding now. To let apply act on them, "
        f"add `include_capture_capsules: true` to {policy_path}."
    )


def _as_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def matches(binding: RetentionBinding, item: SweepItem, now: datetime) -> bool:
    """True when *item* satisfies every predicate of ``binding.match`` (ANDed)."""
    m = binding.match
    if m.asset is not None:
        patterns = _as_list(m.asset)
        if not any(
            fnmatch.fnmatchcase(aid, pat) for aid in item.asset_ids for pat in patterns
        ):
            return False
    if m.deployment_environment is not None:
        envs = _as_list(m.deployment_environment)
        if item.deployment_environment not in envs:
            return False
    if m.tag is not None:
        if not set(_as_list(m.tag)) <= item.tags:
            return False
    if m.age is not None:
        # `age` narrows which items the binding governs (min age); `window`
        # decides when a governed item is due.
        if now < compute_due_at(m.age, item.created_at):
            return False
    return True


def _resolve_action(
    due_bindings: list[RetentionBinding],
) -> tuple[RetentionBinding, RetentionAction] | None:
    """Resolve multi-binding conflicts (spec "Edge cases").

    Returns the (primary binding, action) pair, or ``None`` for a hard
    ``purge`` + ``crypto-shred`` conflict (``ambiguous_binding``). When
    several actions match, the least destructive still-satisfying action wins.
    """
    actions = {b.action for b in due_bindings}
    if RetentionAction.PURGE in actions and RetentionAction.CRYPTO_SHRED in actions:
        return None
    chosen = min(actions, key=lambda a: ACTION_SEVERITY[a])
    primary = next(b for b in due_bindings if b.action == chosen)
    return primary, chosen


def plan_sweep(
    items: list[SweepItem],
    bindings: list[RetentionBinding],
    holds: HoldContext,
    now: datetime | None = None,
) -> list[PlannedDecision]:
    """Compute the sweep plan: one :class:`PlannedDecision` per actionable item.

    Items that match no binding, are not yet due, or are already in their
    terminal state (idempotency, ADR-0134 D2) produce no decision.
    """
    now = now or datetime.now(tz=timezone.utc)
    decisions: list[PlannedDecision] = []
    enabled = [b for b in bindings if b.enabled]
    for item in items:
        matched = [b for b in enabled if matches(b, item, now)]
        due = [b for b in matched if now >= compute_due_at(b.window, item.created_at)]
        if not due:
            continue

        resolved = _resolve_action(due)
        matched_ids = [b.id for b in due]
        if resolved is None:
            first = due[0]
            decisions.append(
                PlannedDecision(
                    item=item,
                    binding_id=first.id,
                    matched_binding_ids=matched_ids,
                    action=first.action,
                    due_at=compute_due_at(first.window, item.created_at),
                    decision=Decision.ERROR,
                    reason="ambiguous_binding",
                    description=first.description,
                )
            )
            continue
        primary, action = resolved
        due_at = compute_due_at(primary.window, item.created_at)

        # Terminal-state no-op (D2): an already-expired item stays expired.
        if action is RetentionAction.EXPIRE_METADATA and item.expired:
            continue

        def _make(
            decision: Decision,
            reason: str | None = None,
            worm_locked_until: datetime | None = None,
            *,
            _item: SweepItem = item,
            _primary: RetentionBinding = primary,
            _action: RetentionAction = action,
            _due_at: datetime = due_at,
            _matched_ids: list[str] = matched_ids,
        ) -> PlannedDecision:
            return PlannedDecision(
                item=_item,
                binding_id=_primary.id,
                matched_binding_ids=_matched_ids,
                action=_action,
                due_at=_due_at,
                description=_primary.description,
                decision=decision,
                reason=reason,
                worm_locked_until=worm_locked_until,
            )

        # --- D4: holds always win, in fixed precedence -------------------
        if holds.active_hold_ids or primary.legal_hold:
            decisions.append(_make(Decision.HELD, "legal_hold_active"))
            continue
        locked_until = holds.worm_locks.get(item.item_id)
        if locked_until is not None and locked_until > now:
            decisions.append(_make(Decision.HELD, "worm_hold", locked_until))
            continue
        if action is RetentionAction.PURGE and holds.deletion_mode == "prohibited":
            decisions.append(_make(Decision.HELD, "deletion_prohibited"))
            continue

        decisions.append(_make(Decision.DUE))
    return decisions
