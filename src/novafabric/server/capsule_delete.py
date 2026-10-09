"""Governed capsule deletion core (ADR-0206 P1, experimental).

One shared helper for the ``/v0`` delete surface — single
``DELETE /v0/capsules/{run_id}`` and ``POST /v0/capsules/bulk-delete`` both
run this pipeline per item, so their semantics cannot drift (serve's
``DELETE /api/runs/{run_id}`` converges here in P2):

1. path-safety check on ``run_id`` (never a traversal component);
2. existence check against the capsule directory (source of truth);
3. **legal holds always win** — any unreleased hold in any registry's
   ``holds.jsonl`` refuses with ``legal_hold_active``; there is no force
   override (parity with serve and ``retention/sweep.py`` D4). Honest limit:
   holds are registry-global today, so one active hold blocks all deletion;
4. WORM refusal — an unexpired ``LocalWormAdapter`` lock for the item
   refuses with ``worm_hold`` (best-effort: unreadable WORM DBs are skipped
   with a warning, matching ``HoldContext``'s "where known" contract);
5. sealed-capsule refusal — a NovaSeal-sealed capsule (``.seal/`` present)
   refuses with ``sealed_capsule`` unless ``NOVAFABRIC_ALLOW_SEALED_DELETE=1``
   (the same default as ``metadata_store.run_delete``);
6. removal of ``capsule_dir/<run_id>`` plus **both** derived indexes: the
   registry runs-cache / content-search rows (ADR-0204) and, when a
   ``store`` + ``tenant_id`` are given, the MetadataStore rows via
   ``MetadataStore.delete_run`` (ADR-0206 P2). Ordering and recovery are
   defined on :func:`execute_delete`.

Audit entries are appended by the route layer (deletion is evidence,
ADR-0134) — this module only decides and executes.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from novafabric.server import capsule_index

if TYPE_CHECKING:
    from novafabric.audit import AuditLog
    from novafabric.metadata_store.interface import MetadataStore

logger = logging.getLogger(__name__)

#: Subdirectory a NovaSeal seal lives in (``capsule/_manifest_write.SEAL_DIR_NAME``).
SEAL_DIR_NAME = ".seal"

#: Explicit policy opt-out: ``1`` lets a sealed capsule be deleted (default: refuse).
ALLOW_SEALED_ENV = "NOVAFABRIC_ALLOW_SEALED_DELETE"

#: Sibling of the capsule dir (``<capsule_dir>/../.deleting``) holding capsules
#: that are mid-delete. Outside ``capsule_dir`` so no scanner mistakes a
#: tombstone for a live capsule.
TOMBSTONE_DIRNAME = ".deleting"

#: A tombstone older than this is residue of a crashed delete, safe to reap.
TOMBSTONE_REAP_AFTER_S = 3600.0

#: Suffix of a tombstone whose delete failed AND whose rollback failed. Such a
#: tombstone holds the capsule's only copy: the reaper never touches it (its
#: name no longer parses as ``<run_id>.<epoch>.<hex>``) and only an operator
#: removes or restores it.
INCONSISTENT_SUFFIX = ".inconsistent"


class DeleteBlockedError(Exception):
    """Deletion refused by a legal hold or WORM lock (→ 409 per item)."""

    def __init__(self, message: str, code: str, details: dict[str, object]) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class DeleteFailedError(Exception):
    """A delete failed midway (→ 500). ``code`` says what state was left.

    ``delete_failed`` — nothing observable changed (rolled back);
    ``delete_inconsistent`` — rollback itself failed; ``details`` names the
    tombstone (``*.inconsistent``, never reaped) holding the capsule bytes so
    an operator can restore it; ``delete_inconsistent_pending`` — a retry was
    refused because such a tombstone already exists for the run id.
    """

    def __init__(self, message: str, code: str, details: dict[str, object]) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class DeleteOutcome:
    """What :func:`execute_delete` did."""

    metadata_rows_removed: int = 0
    #: Set when the index is clean and the capsule is hidden but its bytes could
    #: not be removed yet; reaped on a later delete after the grace period.
    residue: str | None = None


def sealed_capsule(capsule_dir: Path, run_id: str) -> bool:
    """True when the capsule carries a NovaSeal seal directory."""
    return (capsule_dir / run_id / SEAL_DIR_NAME).is_dir()


def sealed_delete_allowed() -> bool:
    """Policy opt-out for sealed capsules (``NOVAFABRIC_ALLOW_SEALED_DELETE=1``)."""
    return os.environ.get(ALLOW_SEALED_ENV, "").strip() == "1"


def chained_audit_log() -> AuditLog:
    """The hash-chained audit log, at :func:`novafabric.audit.resolve_audit_log_path`."""
    from novafabric.audit import AuditLog, resolve_audit_log_path

    return AuditLog(resolve_audit_log_path())


def audit_index_event(
    refused: bool,
    actor: str,
    resource_id: str,
    details: dict[str, Any],
    audit_log: AuditLog | None = None,
) -> None:
    """Append ``run.index_delete`` / ``run.index_delete_refused``. Never raises.

    The route layer also writes the serve audit entry; a failure of this
    second, chained record is logged loudly but must not undo a delete.
    """
    from novafabric.audit import AuditEventType

    try:
        (audit_log or chained_audit_log()).append(
            event_type=(
                AuditEventType.RUN_INDEX_DELETE_REFUSED
                if refused
                else AuditEventType.RUN_INDEX_DELETE
            ),
            actor=actor,
            resource_id=resource_id,
            details=details,
        )
    except Exception:  # noqa: BLE001 — see docstring
        logger.error("chained audit write failed for %s", resource_id, exc_info=True)


def is_valid_run_id(run_id: str) -> bool:
    """True when *run_id* is non-empty and can never traverse the store."""
    return bool(run_id) and "/" not in run_id and "\\" not in run_id and ".." not in run_id


def capsule_exists(capsule_dir: Path, run_id: str) -> bool:
    """True when the capsule directory (source of truth) holds this run."""
    candidate = capsule_dir / run_id
    return candidate.is_dir() and (candidate / "capsule.yaml").exists()


def active_hold_ids(capsule_dir: Path) -> list[str]:
    """Unreleased hold ids across every registry's ``holds.jsonl``.

    Same files serve's delete route and ``cli/retention.py`` read:
    ``<capsule_dir>/../registries/*/holds.jsonl``, active ⇔
    ``released_at is None``. Holds are registry-global (spec §Legal-hold).
    """
    holds: list[str] = []
    registries_base = capsule_dir.parent / "registries"
    if not registries_base.exists():
        return holds
    for reg_dir in sorted(registries_base.iterdir()):
        holds_path = reg_dir / "holds.jsonl"
        if not reg_dir.is_dir() or not holds_path.exists():
            continue
        for line in holds_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                h = json.loads(line)
            except json.JSONDecodeError:
                # Fail closed: a corrupt line may encode an *active* hold, so
                # treat it as a blocking hold rather than silently dropping it
                # (which would make a held capsule deletable). Surface it too.
                logger.warning(
                    "Corrupt line in %s — treating as an active hold to fail "
                    "closed on deletion.",
                    holds_path,
                )
                holds.append(f"__corrupt__:{reg_dir.name}")
                continue
            if h.get("released_at") is None:
                holds.append(str(h["hold_id"]))
    return holds


def worm_locked_until(capsule_dir: Path, run_id: str) -> datetime | None:
    """Unexpired WORM lock expiry for *run_id*, or None.

    Scans ``<capsule_dir>/../registries/*/worm.db`` (the retention CLI's
    layout). Best-effort: a registry whose WORM DB cannot be read is skipped
    with a warning — the check covers locks the machinery *knows*.
    """
    registries_base = capsule_dir.parent / "registries"
    if not registries_base.exists():
        return None
    now = datetime.now(timezone.utc)
    for reg_dir in sorted(registries_base.iterdir()):
        worm_db = reg_dir / "worm.db"
        if not reg_dir.is_dir() or not worm_db.exists():
            continue
        try:
            from novafabric.storage._local_worm import LocalWormAdapter

            for entry in LocalWormAdapter(worm_db).list():
                if entry.capsule_id != run_id:
                    continue
                # WormEntry.locked_until is tz-aware by model contract.
                if entry.locked_until > now:
                    return entry.locked_until
        except Exception:  # noqa: BLE001 — best-effort scan, never blocks
            logger.warning("WORM DB %s unreadable during delete check", worm_db)
    return None


def check_deletable(
    capsule_dir: Path, run_id: str, *, allow_sealed: bool | None = None
) -> None:
    """Raise :class:`DeleteBlockedError` if a hold, WORM lock or seal refuses.

    Holds always win — no flag bypasses this check (ADR-0206 D2). *allow_sealed*
    defaults to :func:`sealed_delete_allowed`; it never overrides a hold or WORM.
    """
    holds = active_hold_ids(capsule_dir)
    if holds:
        raise DeleteBlockedError(
            f"Deletion blocked by {len(holds)} active legal hold(s).",
            code="legal_hold_active",
            details={"hold_ids": holds[:3]},
        )
    locked_until = worm_locked_until(capsule_dir, run_id)
    if locked_until is not None:
        raise DeleteBlockedError(
            f"Deletion blocked by an unexpired WORM lock on '{run_id}'.",
            code="worm_hold",
            details={"locked_until": locked_until.isoformat()},
        )
    if allow_sealed is None:
        allow_sealed = sealed_delete_allowed()
    if not allow_sealed and sealed_capsule(capsule_dir, run_id):
        raise DeleteBlockedError(
            f"Deletion blocked: '{run_id}' is NovaSeal-sealed.",
            code="sealed_capsule",
            details={"seal_dir": SEAL_DIR_NAME, "override_env": ALLOW_SEALED_ENV},
        )


def inconsistent_tombstones(capsule_dir: Path, run_id: str) -> list[Path]:
    """Never-reaped ``<run_id>.<epoch>.<hex>.inconsistent`` tombstones for *run_id*."""
    base = capsule_dir.parent / TOMBSTONE_DIRNAME
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    found: list[Path] = []
    for entry in entries:
        if not entry.name.endswith(INCONSISTENT_SUFFIX):
            continue
        parts = entry.name[: -len(INCONSISTENT_SUFFIX)].rsplit(".", 2)
        if len(parts) == 3 and parts[0] == run_id:
            found.append(entry)
    return found


def unfinished_delete_run_ids(audit_path: Path | None = None) -> set[str] | None:
    """Run ids with a ``capsule_delete_failed`` audit entry and no later ``capsule_delete``.

    Reads the dashboard mutation audit log in order. A missing file means no
    failures were ever recorded (empty set). ``None`` means *cannot tell*
    (unreadable file or a malformed line) -- callers must then fail safe.
    """
    from novafabric._paths import dashboard_audit_path  # noqa: PLC0415

    path = audit_path if audit_path is not None else dashboard_audit_path()
    pending: set[str] = set()
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                entry = json.loads(raw)
                action = entry.get("action")
                rid = (entry.get("args") or {}).get("run_id")
                if not isinstance(rid, str):
                    continue
                if action == "capsule_delete_failed":
                    pending.add(rid)
                elif action == "capsule_delete":
                    pending.discard(rid)
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return pending


def _run_still_referenced(
    run_id: str,
    conn: sqlite3.Connection | None,
    store: MetadataStore | None,
    tenant_id: UUID | None,
) -> bool | None:
    """True/False when the run has (no) index row; ``None`` when it cannot be told."""
    try:
        if conn is not None:
            row = conn.execute(
                "SELECT 1 FROM runs_cache WHERE run_id = ? LIMIT 1", (run_id,)
            ).fetchone()
            if row is not None:
                return True
        if store is not None and tenant_id is not None:
            try:
                rid = UUID(run_id)
            except ValueError:
                return False  # not a UUID run: the store cannot hold it
            with store.begin_tenant_context(tenant_id) as ctx:
                if ctx.lookup_run(rid, tenant_id) is not None:
                    return True
    except Exception:  # noqa: BLE001 - unreadable index: fail safe
        return None
    return False


def reap_tombstones(
    capsule_dir: Path,
    *,
    now: float | None = None,
    conn: sqlite3.Connection | None = None,
    store: MetadataStore | None = None,
    tenant_id: UUID | None = None,
    audit_path: Path | None = None,
) -> int:
    """Remove residue of crashed deletes older than the grace period.

    Tombstones are named ``<run_id>.<epoch>.<hex>``; only the epoch in the name
    is trusted (a rename does not touch mtime), and a young one may belong to a
    live sibling worker that can still roll back, so it is left alone.
    ``*.inconsistent`` tombstones (see :data:`INCONSISTENT_SUFFIX`) hold the only
    copy of a capsule and are **never** reaped, whatever their age.

    An old plain tombstone is also kept while its run is still referenced: a
    runs-cache row (*conn*), a MetadataStore row (*store* + *tenant_id*), or an
    unfinished-delete audit entry (:func:`unfinished_delete_run_ids`) means the
    delete never completed and the tombstone may be the only copy (the marking
    and rollback renames both failed). If either source cannot be read the
    bytes are kept. Only orphans whose index rows are gone are reaped.
    Fail-open on the directory scan; returns the number removed.
    """
    base = capsule_dir.parent / TOMBSTONE_DIRNAME
    cutoff = (time.time() if now is None else now) - TOMBSTONE_REAP_AFTER_S
    removed = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    unfinished: set[str] | None = None
    unfinished_loaded = False
    for entry in entries:
        if entry.name.endswith(INCONSISTENT_SUFFIX):
            continue
        try:
            parts = entry.name.rsplit(".", 2)
            epoch = float(parts[1])
            run_id = parts[0]
        except (IndexError, ValueError):
            continue
        if not (epoch < cutoff and entry.is_dir()):
            continue
        if not unfinished_loaded:
            unfinished, unfinished_loaded = unfinished_delete_run_ids(audit_path), True
        if unfinished is None or run_id in unfinished:
            continue  # unfinished delete (or audit unreadable): keep the bytes
        if _run_still_referenced(run_id, conn, store, tenant_id) is not False:
            continue  # indexed, or index unreadable: keep the bytes
        shutil.rmtree(entry, ignore_errors=True)
        removed += not entry.exists()
    return removed


def execute_delete(
    capsule_dir: Path,
    run_id: str,
    conn: sqlite3.Connection | None,
    *,
    store: MetadataStore | None = None,
    tenant_id: UUID | None = None,
    actor: str = "system",
    audit_log: AuditLog | None = None,
) -> DeleteOutcome:
    """Remove the capsule and both derived indexes, in a recoverable order.

    *conn* is the registry-DB connection (None skips runs-cache cleanup — the
    lazy ``sync_index`` prune repairs it). *store* + *tenant_id* (a UUID
    *run_id* only) additionally drop the MetadataStore rows.

    Ordering (the capsule directory is the source of truth, so it is never
    destroyed while an index step can still fail):

    1. **tombstone** — atomically rename ``capsule_dir/<run_id>`` into
       ``<capsule_dir>/../.deleting/``. The capsule is now invisible to every
       reader. A failure here changes nothing (``delete_failed``).
    2. **index rows** — runs-cache, then MetadataStore. On any failure the
       tombstone is renamed back and the runs-cache re-synced, so capsule and
       index agree again (``delete_failed``); if the rename-back itself fails
       the error is ``delete_inconsistent`` and names the tombstone. To stay
       correct if the process dies mid-failure, the tombstone is renamed to
       ``<tombstone>.inconsistent`` (invisible to the reaper) **before** the
       rename-back is attempted, so there is no window in which the only copy
       is a reapable name. A crash after that leaves a safe, operator-visible
       ``.inconsistent`` tombstone. If that marking rename itself fails *and*
       the rename-back fails, the plain tombstone is reported, and
       :func:`reap_tombstones` still refuses to remove it while the run has an
       index row or an unfinished-delete audit entry.
       While an ``.inconsistent`` tombstone exists for the run id, a new delete
       is refused (``delete_inconsistent_pending``) rather than adding a second.
    3. **purge** — ``rmtree`` the tombstone. The delete has already happened
       logically; a failure leaves a hidden, unindexed residue (reported in
       :class:`DeleteOutcome`, reaped by :func:`reap_tombstones` later), never a
       visible capsule without its index or the reverse.

    One ``run.index_delete`` audit entry is written when MetadataStore rows
    were actually removed.
    """
    reap_tombstones(capsule_dir, conn=conn, store=store, tenant_id=tenant_id)
    pending = inconsistent_tombstones(capsule_dir, run_id)
    if pending:
        raise DeleteFailedError(
            f"refusing to delete '{run_id}': a previous delete failed and its rollback "
            f"failed; the capsule bytes are at {pending[0]} (never reaped). Restore "
            f"them (rename back to '{capsule_dir / run_id}') or remove that directory "
            f"by hand, then retry",
            code="delete_inconsistent_pending",
            details={"stage": "pending", "tombstone": str(pending[0])},
        )
    src = capsule_dir / run_id
    tomb_dir = capsule_dir.parent / TOMBSTONE_DIRNAME
    tomb = tomb_dir / f"{run_id}.{int(time.time())}.{secrets.token_hex(4)}"
    try:
        tomb_dir.mkdir(parents=True, exist_ok=True)
        os.rename(src, tomb)
    except OSError as exc:
        raise DeleteFailedError(
            f"could not remove capsule '{run_id}': {exc}",
            code="delete_failed",
            details={"stage": "tombstone"},
        ) from exc

    removed_rows = 0
    try:
        if conn is not None:
            try:
                capsule_index.remove_run(conn, run_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if store is not None and tenant_id is not None:
            try:
                rid: UUID | None = UUID(run_id)
            except ValueError:
                rid = None  # not a UUID run: the store cannot hold it
            if rid is not None:
                with store.begin_tenant_context(tenant_id) as ctx:
                    removed_rows = int(ctx.delete_run(rid, tenant_id))
    except Exception as exc:
        # Mark first (see "Ordering" above): the name must be un-reapable before
        # any step that can fail leaves it as the only copy.
        marked = tomb.with_name(tomb.name + INCONSISTENT_SUFFIX)
        try:
            os.rename(tomb, marked)
            held = marked
        except OSError:
            held = tomb  # marking failed; fall through to the plain rollback
        try:
            os.rename(held, src)
        except OSError as rb_exc:
            logger.error("rollback of %s failed; bytes remain at %s", run_id, held)
            raise DeleteFailedError(
                f"index delete failed ({exc}) and rollback failed ({rb_exc}); "
                f"capsule bytes are at {held}"
                + (
                    f" (never reaped; restore by renaming it back to '{src}')"
                    if held == marked
                    else " (NOT protected from reaping: restore it within an hour)"
                ),
                code="delete_inconsistent",
                details={"stage": "rollback", "tombstone": str(held)},
            ) from exc
        if conn is not None:
            try:
                capsule_index.sync_index(conn, capsule_dir)  # re-heal runs-cache
            except Exception:  # noqa: BLE001 — next list request backfills
                logger.warning("runs-cache resync after rollback failed", exc_info=True)
        raise DeleteFailedError(
            f"index delete failed, capsule '{run_id}' restored: {exc}",
            code="delete_failed",
            details={"stage": "index"},
        ) from exc

    residue: str | None = None
    try:
        shutil.rmtree(tomb)
    except OSError:
        logger.warning("capsule %s deleted but residue remains at %s", run_id, tomb)
        residue = str(tomb)
    if removed_rows:
        audit_index_event(
            False,
            actor,
            run_id,
            {"tenant_id": str(tenant_id), "scope": "metadata_index", "via": "capsule_delete"},
            audit_log,
        )
    return DeleteOutcome(metadata_rows_removed=removed_rows, residue=residue)
