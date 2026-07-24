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
"""Retention sweep executor (ADR-0134 D5/D6): dispatch + evidence.

Every action the sweep takes — and every skip — is appended to the
hash-chained audit log (ADR-0031 §3) as a :class:`RetentionActionRecord`
carried inside an :class:`novafabric.audit.AuditEntry`. The sweep never
destroys data without leaving a record that it did so and why.

The executor owns **no** deletion mechanism: ``crypto-shred`` dispatches to
the existing ADR-0069 :class:`~novafabric.pii.dek.DEKStore` (never
reimplemented), ``purge`` removes the local capsule directory subject to the
ADR-0031 gates enforced by the planner, and ``expire-metadata`` writes a
marker file, leaving the capsule object untouched.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from novafabric.audit import AuditEventType, AuditLog
from novafabric.pii.dek import DEKStore, ErasureDeferredReceipt, ErasureReceipt
from novafabric.retention.models import (
    Decision,
    PlannedDecision,
    RetentionAction,
    RetentionActionRecord,
    SweepOutcome,
)
from novafabric.retention.sweep import EXPIRED_MARKER

log = logging.getLogger(__name__)


class SweepExecutor:
    """Applies a sweep plan one item at a time, bounded and fail-safe (D6).

    A failure on item *N* is recorded ``outcome: error`` and the sweep
    continues to *N+1*; a scheduler failure never harms capsules or blocks
    the user workload. Dry-run performs no action, emits no receipt, and
    appends no audit record (D3).
    """

    def __init__(
        self,
        *,
        registry: str,
        principal: str,
        audit_log: AuditLog | None,
        dek_store: DEKStore | None = None,
        receipt_dir: Path | None = None,
        retention_months: int = 6,
    ) -> None:
        self._registry = registry
        self._principal = principal
        self._audit_log = audit_log
        self._dek_store = dek_store
        self._receipt_dir = receipt_dir
        self._retention_months = retention_months

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        plan: list[PlannedDecision],
        *,
        dry_run: bool,
        limit: int | None = None,
    ) -> list[RetentionActionRecord]:
        """Execute (or preview) *plan*; return one record per decision.

        ``limit`` bounds the number of *applied* actions in one pass; held and
        errored decisions are always recorded (never silently dropped).
        """
        records: list[RetentionActionRecord] = []
        applied_count = 0
        for decision in plan:
            if decision.decision is Decision.DUE:
                if limit is not None and applied_count >= limit:
                    continue  # carried to the next pass (D2)
                applied_count += 1
            record = self._dispatch(decision, dry_run=dry_run)
            records.append(record)
            if not dry_run and self._audit_log is not None:
                self._audit_log.append(
                    event_type=AuditEventType.RETENTION_ACTION,
                    actor=self._principal,
                    resource_id=record.item_id,
                    details=record.model_dump(mode="json", exclude_none=True),
                )
        return records

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _record(
        self,
        decision: PlannedDecision,
        outcome: SweepOutcome,
        reason: str | None = None,
        erasure_receipt_ref: str | None = None,
    ) -> RetentionActionRecord:
        # ADR-0137 lifecycle event. Only the APPLIED outcome counts as
        # "retention applied": emitting for SKIPPED/HELD/DRY_RUN/ERROR would
        # make a consumer counting deletions over-count, which is the number a
        # retention consumer is most likely to trust.
        if outcome is SweepOutcome.APPLIED:
            from novafabric.events.lifecycle_sources import (  # noqa: PLC0415
                emit_retention_applied,
            )

            emit_retention_applied(
                item_id=decision.item.item_id,
                item_kind=str(decision.item.item_kind),
                action=str(decision.action),
                binding_id=decision.binding_id,
                reason=reason if reason is not None else decision.reason,
            )

        return RetentionActionRecord(
            swept_at=datetime.now(tz=timezone.utc),
            principal=self._principal,
            registry=self._registry,
            binding_id=decision.binding_id,
            item_kind=decision.item.item_kind,
            item_id=decision.item.item_id,
            action=decision.action,
            outcome=outcome,
            due_at=decision.due_at,
            reason=reason if reason is not None else decision.reason,
            worm_locked_until=decision.worm_locked_until,
            erasure_receipt_ref=erasure_receipt_ref,
            description=decision.description,
        )

    def _dispatch(
        self, decision: PlannedDecision, *, dry_run: bool
    ) -> RetentionActionRecord:
        if decision.decision is Decision.HELD:
            return self._record(decision, SweepOutcome.SKIPPED)
        if decision.decision is Decision.ERROR:
            return self._record(decision, SweepOutcome.ERROR)
        if dry_run:
            return self._record(decision, SweepOutcome.DRY_RUN)

        try:
            if decision.action is RetentionAction.EXPIRE_METADATA:
                return self._apply_expire(decision)
            if decision.action is RetentionAction.PURGE:
                return self._apply_purge(decision)
            return self._apply_crypto_shred(decision)
        except Exception as exc:  # noqa: BLE001 — fail-safe sweep (D6)
            log.warning(
                "retention: action %s failed on %s: %s",
                decision.action.value,
                decision.item.item_id,
                exc,
            )
            return self._record(
                decision, SweepOutcome.ERROR, reason=f"backend_error: {exc}"
            )

    # ------------------------------------------------------------------
    # Actions (each dispatches to an existing mechanism)
    # ------------------------------------------------------------------

    def _apply_expire(self, decision: PlannedDecision) -> RetentionActionRecord:
        path = decision.item.path
        if path is None or not path.is_dir():
            return self._record(decision, SweepOutcome.APPLIED, reason="already_absent")
        marker = path / EXPIRED_MARKER
        if marker.exists():  # terminal no-op (D2)
            return self._record(decision, SweepOutcome.APPLIED, reason="already_expired")
        marker.write_text(
            json.dumps(
                {
                    "expired_at": datetime.now(tz=timezone.utc).isoformat(),
                    "binding_id": decision.binding_id,
                    "registry": self._registry,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return self._record(decision, SweepOutcome.APPLIED)

    def _apply_purge(self, decision: PlannedDecision) -> RetentionActionRecord:
        path = decision.item.path
        if path is None or not path.is_dir():
            return self._record(decision, SweepOutcome.APPLIED, reason="already_absent")
        shutil.rmtree(path)
        return self._record(decision, SweepOutcome.APPLIED)

    def _apply_crypto_shred(self, decision: PlannedDecision) -> RetentionActionRecord:
        subject_id = decision.item.pii_subject_id
        if subject_id is None or self._dek_store is None:
            # No PII subject / no DEK store: no-op erasure (spec edge case).
            return self._record(decision, SweepOutcome.APPLIED, reason="no_dek")
        try:
            result = self._dek_store.erase_subject(
                subject_id=subject_id,
                capsule_ids=[decision.item.item_id],
                retention_months=self._retention_months,
            )
        except KeyError:
            # Already shredded or never registered: idempotent no-op (D2).
            return self._record(decision, SweepOutcome.APPLIED, reason="no_dek")
        receipt_ref = self._write_receipt(decision.item.item_id, result)
        if isinstance(result, ErasureDeferredReceipt):
            return self._record(
                decision,
                SweepOutcome.DEFERRED,
                reason="art17_retention_window",
                erasure_receipt_ref=receipt_ref,
            )
        return self._record(
            decision, SweepOutcome.APPLIED, erasure_receipt_ref=receipt_ref
        )

    def _write_receipt(
        self, item_id: str, result: ErasureReceipt | ErasureDeferredReceipt
    ) -> str | None:
        if self._receipt_dir is None:
            return None
        self._receipt_dir.mkdir(parents=True, exist_ok=True)
        suffix = (
            "deferred.receipt.json"
            if isinstance(result, ErasureDeferredReceipt)
            else "receipt.json"
        )
        path = self._receipt_dir / f"{item_id}.{suffix}"
        path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return str(path)
