"""Governed run deletion from the metadata index (ADR-0206 P2, experimental).

:meth:`MetadataStore.delete_run` is the mechanism (index-only, no policy). This
module is the policy that every caller outside tests must go through:

* **Refuse, never skip.** Before anything is deleted every requested run is
  checked; if *any* is blocked the whole request is refused with a
  :class:`RunDeleteRefusedError` that names each blocked run and why, and **no
  row is removed**. A held run is never silently left behind while its siblings
  go (that would make a partial result look like a success).
* **Legal holds always win** (ADR-0031/0134, parity with ``DELETE /v0/capsules``):
  any unreleased hold in any registry's ``holds.jsonl`` refuses the request, with
  no override. Holds are registry-global today, so one active hold blocks every
  run — recorded honestly, not hidden. An unreadable hold line is a hold (fail
  closed).
* **WORM** — an unexpired lock the retention machinery knows refuses that run.
* **Sealed-capsule immutability** — this code never writes to or removes anything
  inside a capsule directory. A run whose capsule is NovaSeal-sealed (``.seal/``
  present) is additionally refused by default: dropping its index row while the
  sealed evidence stays on disk is allowed only with an explicit
  ``allow_sealed=True`` (the row is rebuildable from the capsule).
* **Audit** — deletion is evidence (ADR-0134): one ``run.index_delete`` entry per
  run removed, one summary entry for a multi-run request, and one
  ``run.index_delete_refused`` entry for a refused request, all in the chained
  audit log.

Scope: the **index only**. It does not delete capsules (use the governed
``DELETE /v0/capsules`` pipeline in ``server/capsule_delete.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from novafabric.audit import AuditEventType, AuditLog, resolve_audit_log_path
from novafabric.metadata_store.interface import MetadataStore
from novafabric.server.capsule_delete import active_hold_ids, worm_locked_until

logger = logging.getLogger(__name__)

#: Hard ceiling per request (matches ``server.bulk.max_items``'s ceiling, ADR-0206 D3).
MAX_RUNS_PER_REQUEST = 1000

#: Subdirectory a NovaSeal seal lives in (``capsule/_manifest_write.SEAL_DIR_NAME``).
SEAL_DIR_NAME = ".seal"


@dataclass(frozen=True)
class RunRefusal:
    """Why one run (or, for a hold, every run) cannot be deleted."""

    run_id: str
    code: str  # legal_hold_active | worm_hold | sealed_capsule
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "code": self.code, "details": dict(self.details)}


class RunDeleteRefusedError(Exception):
    """The request was refused as a whole; nothing was deleted."""

    def __init__(self, refusals: list[RunRefusal]) -> None:
        self.refusals = refusals
        codes = sorted({r.code for r in refusals})
        super().__init__(
            f"run deletion refused for {len(refusals)} run(s) ({', '.join(codes)}); "
            "nothing was deleted"
        )


@dataclass(frozen=True)
class DeleteRunsReport:
    """Outcome of an accepted request."""

    requested: list[str]
    deleted: list[str]
    absent: list[str]  # not in the index (already gone) — idempotent, not an error
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": list(self.requested),
            "deleted": list(self.deleted),
            "absent": list(self.absent),
            "dry_run": self.dry_run,
        }


def _normalise(run_ids: Iterable[UUID | str]) -> list[UUID]:
    """Validate and de-duplicate (order-preserving); a bad id fails the whole request."""
    out: list[UUID] = []
    seen: set[UUID] = set()
    for raw in run_ids:
        try:
            rid = raw if isinstance(raw, UUID) else UUID(str(raw))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(f"invalid run_id {raw!r}: not a UUID") from exc
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    if not out:
        raise ValueError("no run ids given")
    if len(out) > MAX_RUNS_PER_REQUEST:
        raise ValueError(
            f"{len(out)} runs exceeds the per-request ceiling of {MAX_RUNS_PER_REQUEST}"
        )
    return out


def check_runs_deletable(
    run_ids: list[UUID], *, capsule_dir: Path, allow_sealed: bool = False
) -> list[RunRefusal]:
    """Every refusal that applies to ``run_ids`` (empty list = all deletable). Read-only."""
    refusals: list[RunRefusal] = []
    holds = active_hold_ids(capsule_dir)
    for rid in run_ids:
        name = str(rid)
        if holds:
            refusals.append(
                RunRefusal(name, "legal_hold_active", {"hold_ids": holds[:3], "count": len(holds)})
            )
            continue  # a hold is decisive; no need to also report the lesser reasons
        locked = worm_locked_until(capsule_dir, name)
        if locked is not None:
            refusals.append(RunRefusal(name, "worm_hold", {"locked_until": locked.isoformat()}))
            continue
        if not allow_sealed and (capsule_dir / name / SEAL_DIR_NAME).is_dir():
            refusals.append(RunRefusal(name, "sealed_capsule", {"seal_dir": SEAL_DIR_NAME}))
    return refusals


def delete_runs(
    store: MetadataStore,
    tenant_id: UUID,
    run_ids: Iterable[UUID | str],
    *,
    capsule_dir: Path,
    actor: str,
    allow_sealed: bool = False,
    dry_run: bool = False,
    audit_log: AuditLog | None = None,
) -> DeleteRunsReport:
    """Delete one or many runs from the metadata index under hold/WORM/seal policy.

    Raises:
        ValueError: empty request, a non-UUID id, or more than
            :data:`MAX_RUNS_PER_REQUEST` runs (nothing is touched).
        RunDeleteRefusedError: any run is held, WORM-locked or (by default)
            sealed — **nothing** is deleted and a refusal audit entry is written.
        NotImplementedError: the store does not implement ``delete_run``.

    The index rows go in one tenant context, so on Postgres a multi-run request is
    a single transaction (all-or-nothing); SQLite deletes per run (dev-only). Audit
    entries are written after the rows are removed; an audit write failure
    propagates — the caller must treat the request as completed-but-unaudited.
    """
    ids = _normalise(run_ids)
    log = audit_log if audit_log is not None else AuditLog(resolve_audit_log_path())
    requested = [str(r) for r in ids]

    refusals = check_runs_deletable(ids, capsule_dir=capsule_dir, allow_sealed=allow_sealed)
    if refusals:
        log.append(
            event_type=AuditEventType.RUN_INDEX_DELETE_REFUSED,
            actor=actor,
            resource_id=requested[0] if len(requested) == 1 else f"batch:{len(requested)}",
            details={
                "tenant_id": str(tenant_id),
                "requested": requested,
                "refusals": [r.to_dict() for r in refusals],
            },
        )
        raise RunDeleteRefusedError(refusals)

    if dry_run:
        return DeleteRunsReport(requested, [], [], dry_run=True)

    deleted: list[str] = []
    absent: list[str] = []
    with store.begin_tenant_context(tenant_id) as ctx:
        for rid in ids:
            (deleted if ctx.delete_run(rid, tenant_id) else absent).append(str(rid))

    for name in deleted:
        log.append(
            event_type=AuditEventType.RUN_INDEX_DELETE,
            actor=actor,
            resource_id=name,
            details={"tenant_id": str(tenant_id), "scope": "metadata_index"},
        )
    if len(requested) > 1:
        log.append(
            event_type=AuditEventType.RUN_INDEX_DELETE,
            actor=actor,
            resource_id=f"batch:{len(requested)}",
            details={
                "tenant_id": str(tenant_id),
                "scope": "metadata_index",
                "summary": True,
                "requested": len(requested),
                "deleted": len(deleted),
                "absent": len(absent),
            },
        )
    return DeleteRunsReport(requested, deleted, absent)
