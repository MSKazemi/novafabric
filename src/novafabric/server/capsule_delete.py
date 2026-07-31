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
5. removal of ``capsule_dir/<run_id>`` plus derived-index cleanup
   (runs-cache row, content-search rows — ADR-0204 per-run delete). The
   MetadataStore has no ``delete_run`` today; its best-effort rows may go
   stale, which its "derived, rebuildable" doctrine permits (ADR-0206
   Consequences).

Audit entries are appended by the route layer (deletion is evidence,
ADR-0134) — this module only decides and executes.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from novafabric.server import capsule_index

logger = logging.getLogger(__name__)


class DeleteBlockedError(Exception):
    """Deletion refused by a legal hold or WORM lock (→ 409 per item)."""

    def __init__(self, message: str, code: str, details: dict[str, object]) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


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


def check_deletable(capsule_dir: Path, run_id: str) -> None:
    """Raise :class:`DeleteBlockedError` if a hold or WORM lock refuses.

    Holds always win — no flag bypasses this check (ADR-0206 D2).
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


def execute_delete(
    capsule_dir: Path, run_id: str, conn: sqlite3.Connection | None
) -> None:
    """Remove the capsule directory and its derived index rows.

    *conn* is the registry-DB connection (None skips index cleanup — the
    lazy ``sync_index`` prune repairs it on the next list request).
    """
    shutil.rmtree(capsule_dir / run_id)
    if conn is not None:
        capsule_index.remove_run(conn, run_id)
        conn.commit()
