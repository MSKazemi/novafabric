from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.adapters.git import get_head_sha
from novafabric.audit import AUDIT_LOG_PATH, AuditEventType, AuditLog
from novafabric.policy import (
    PolicyInput,
    PolicyResource,
    PolicySubject,
    get_policy_engine,
)
from novafabric.registry.store import get_connection, init_schema
from novafabric.spec.models import AssetStatus, AssetType, BaseAssetSpec

_VALID_TRANSITIONS: dict[AssetStatus, set[AssetStatus]] = {
    AssetStatus.development: {
        AssetStatus.validated,
        AssetStatus.staging,
        AssetStatus.production,
        AssetStatus.archived,
    },
    AssetStatus.validated: {
        AssetStatus.pending_approval,
        AssetStatus.staging,
        AssetStatus.production,
        AssetStatus.archived,
    },
    AssetStatus.pending_approval: {
        AssetStatus.staging,
        AssetStatus.production,
        AssetStatus.archived,
    },
    AssetStatus.staging: {AssetStatus.production, AssetStatus.archived},
    AssetStatus.production: {AssetStatus.archived},
    AssetStatus.archived: set(),
}

_AGENT_GATE_STATUSES = {AssetStatus.staging, AssetStatus.production}


class DuplicateAssetError(Exception):
    pass


class AssetNotFoundError(Exception):
    pass


class PromotionBlockedError(Exception):
    pass


class InvalidLifecycleTransitionError(Exception):
    pass


class SoDViolationError(Exception):
    pass


class RollbackError(Exception):
    pass


class UnregisterBlockedError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d: dict[str, Any] = {k: row[k] for k in row.keys()}
    d["forced_promotion"] = bool(d.get("forced_promotion"))
    return d


def register_asset(
    spec: BaseAssetSpec,
    spec_path: Path,
    db_path: Path | None = None,
) -> dict[str, Any]:
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        existing = conn.execute(
            "SELECT id FROM assets WHERE name = ? AND version = ?",
            (spec.name, spec.version),
        ).fetchone()
        if existing:
            raise DuplicateAssetError(
                f"Asset '{spec.name}@{spec.version}' is already registered."
            )

        asset_id = str(uuid.uuid4())
        git_sha = get_head_sha()
        spec_json = spec.model_dump_json()

        conn.execute(
            """
            INSERT INTO assets
                (id, name, asset_type, version, status, spec_json,
                 git_commit_sha, created_at, forced_promotion)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                asset_id,
                spec.name,
                spec.asset_type.value,
                spec.version,
                spec.status.value,
                spec_json,
                git_sha,
                _now(),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        result = _row_to_dict(row)

        # C-1.3 — record declared dependency lineage edges
        if spec.dependencies:
            _record_declared_deps(
                asset_name=spec.name,
                asset_version=spec.version,
                dependencies=spec.dependencies,
                db_path=db_path,
            )

        return result
    finally:
        conn.close()


def _record_declared_deps(
    asset_name: str,
    asset_version: str,
    dependencies: list[str],
    db_path: Path | None = None,
) -> None:
    """Write DEPENDS_ON lineage edges for every declared dependency.

    Each edge runs: asset(name@version) --[depends_on]--> asset(dep_ref).
    Edges use confidence=``declared`` and a synthetic capsule_run_id so
    ``blast_radius`` traversal works without a real run being present.
    """
    from novafabric.lineage._store import LineageStore
    from novafabric.lineage._types import LineageEdge

    store = LineageStore(db_path)
    asset_ref = f"{asset_name}@{asset_version}"
    # Synthetic capsule_run_id encodes the registration event so it is
    # stable but recognisable as non-run provenance.
    synthetic_run_id = f"reg:{asset_ref}"

    for dep_ref in dependencies:
        edge = LineageEdge(
            edge_type="depends_on",
            source={
                "kind": "asset",
                "asset_ref": asset_ref,
                "registry": "local",
            },
            target={
                "kind": "asset",
                "asset_ref": dep_ref,
                "registry": "local",
            },
            confidence="declared",
            capsule_run_id=synthetic_run_id,
            facets={"dependency_kind": "declared"},
        )
        store.insert_edge(edge)


def record_asset_consumption(
    asset_ref: str,
    status_at_consumption: str,
    capsule_dir: Path,
    registry: str = "local",
) -> None:
    """Append an asset consumption record to the capsule's assets.jsonl.

    The ``status_at_consumption`` field captures the lifecycle status of
    the asset *at the time the run used it*, enabling blast-radius and
    provenance queries to answer "was this asset promoted when it was
    consumed?".

    Args:
        asset_ref: Asset reference string in ``name@version`` form.
        status_at_consumption: The asset's lifecycle status value (e.g.
            ``"production"``, ``"staging"``, ``"development"``).
        capsule_dir: Path to the active capsule directory.
        registry: Registry identifier (default: ``"local"``).
    """
    record = {
        "asset_ref": asset_ref,
        "registry": registry,
        "status_at_consumption": status_at_consumption,
    }
    assets_path = capsule_dir / "assets.jsonl"
    with open(assets_path, "a") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def list_assets(
    asset_type: str | None,
    status: str | None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        query = "SELECT * FROM assets WHERE 1=1"
        params: list[str] = []
        if asset_type:
            query += " AND asset_type = ?"
            params.append(asset_type)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_assets_paginated(
    asset_type: str | None,
    status: str | None,
    limit: int = 100,
    offset: int = 0,
    db_path: Path | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(page, total)`` from the assets table using SQL pagination.

    Unlike :func:`list_assets` (which fetches every row and returns full specs),
    this selects only the columns the dashboard list view needs — notably
    *omitting* the verbose ``spec_json`` blob — and applies ``LIMIT``/``OFFSET``
    at the SQL layer.  ``total`` is a ``COUNT(*)`` over the same WHERE clause so
    callers can compute ``has_more`` without reading all rows.

    Args:
        asset_type: Optional asset-type filter.
        status: Optional lifecycle-status filter.
        limit: Maximum rows to return in the page.
        offset: Row offset for the page.
        db_path: Registry database path (default: resolved by ``get_connection``).

    Returns:
        A tuple ``(rows, total)`` where ``rows`` is the requested page of
        list-view dicts and ``total`` is the full filtered row count.
    """
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        where = "WHERE 1=1"
        params: list[Any] = []
        if asset_type:
            where += " AND asset_type = ?"
            params.append(asset_type)
        if status:
            where += " AND status = ?"
            params.append(status)
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM assets {where}", params
            ).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT id, name, version, asset_type, status, created_at, "
            f"promoted_at, git_commit_sha FROM assets {where} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        page = [{k: r[k] for k in r.keys()} for r in rows]
        return page, total
    finally:
        conn.close()


def _normalize_version(version: str) -> str:
    """Normalize a semver string: v1 → v1.0.0, 1.0 → 1.0.0."""
    prefix = "v" if version.startswith("v") else ""
    bare = version.lstrip("v")
    parts = bare.split(".")
    while len(parts) < 3:
        parts.append("0")
    return f"{prefix}{'.'.join(parts)}"


def get_asset(
    name: str,
    version: str | None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        # "latest" and None both mean newest.
        effective_version = version if version and version != "latest" else None
        if effective_version:
            row = conn.execute(
                "SELECT * FROM assets WHERE name = ? AND version = ?",
                (name, effective_version),
            ).fetchone()
            # Retry with normalized semver if exact match fails (e.g. v1 → v1.0.0).
            if not row:
                normalized = _normalize_version(effective_version)
                if normalized != effective_version:
                    row = conn.execute(
                        "SELECT * FROM assets WHERE name = ? AND version = ?",
                        (name, normalized),
                    ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM assets WHERE name = ? ORDER BY created_at DESC LIMIT 1",
                (name,),
            ).fetchone()
        if not row:
            raise AssetNotFoundError(f"Asset '{name}@{version or 'latest'}' not found.")
        return _row_to_dict(row)
    finally:
        conn.close()


def _has_passing_eval(conn: sqlite3.Connection, asset_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM eval_results WHERE asset_id = ? AND passed = 1 LIMIT 1",
        (asset_id,),
    ).fetchone()
    return row is not None


def _latest_eval_score(conn: sqlite3.Connection, asset_id: str) -> float | None:
    """Numeric score of the most recent eval run, for the OPA promote gate.

    Falls back to 1.0/0.0 from the pass flag when score_json carries no
    numeric score. None when the asset has never been evaluated.
    """
    row = conn.execute(
        "SELECT passed, score_json FROM eval_results "
        "WHERE asset_id = ? ORDER BY run_at DESC LIMIT 1",
        (asset_id,),
    ).fetchone()
    if row is None:
        return None
    if row["score_json"]:
        try:
            parsed = json.loads(row["score_json"])
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
            return float(parsed)
        if isinstance(parsed, dict):
            score = parsed.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                return float(score)
    return 1.0 if row["passed"] else 0.0


# Default SPRT hypotheses / error budgets for the opt-in significance gate
# (ADR-0080). p0 = acceptable pass-rate (H0), p1 = regression-threshold pass-rate
# (H1, p1 < p0). alpha/beta = false-positive / false-negative budgets.
_SIG_DEFAULT_P0 = 0.9
_SIG_DEFAULT_P1 = 0.7
_SIG_DEFAULT_ALPHA = 0.05
_SIG_DEFAULT_BETA = 0.05


def _recent_eval_sequence(
    conn: sqlite3.Connection, asset_id: str, limit: int = 200
) -> list[int]:
    """Chronological pass/fail (1/0) sequence for an asset's recent evals.

    Returns up to ``limit`` most-recent ``eval_results`` rows ordered oldest→newest
    so a sequential test sees them in arrival order. Empty when never evaluated.
    """
    rows = conn.execute(
        "SELECT passed FROM eval_results WHERE asset_id = ? "
        "ORDER BY run_at DESC LIMIT ?",
        (asset_id, limit),
    ).fetchall()
    # rows are newest-first; reverse to chronological for the sequential test.
    return [1 if r["passed"] else 0 for r in reversed(rows)]


def _significant_regression(
    conn: sqlite3.Connection,
    asset_id: str,
    p0: float = _SIG_DEFAULT_P0,
    p1: float = _SIG_DEFAULT_P1,
    alpha: float = _SIG_DEFAULT_ALPHA,
    beta: float = _SIG_DEFAULT_BETA,
) -> tuple[bool, str]:
    """Decide whether the recent eval sequence shows a *significant* regression.

    Runs the Wald SPRT (ADR-0080) over the recent pass/fail sequence. Returns
    ``(blocked, reason)``: ``blocked`` is True only for ``ACCEPT_H1`` (a
    statistically significant regression). ``ACCEPT_H0`` (no regression) and
    ``CONTINUE`` (inconclusive — defer) never block, so a noisy single-run dip
    cannot fire the gate.
    """
    from novafabric.eval.significance import SprtVerdict, sprt_bernoulli  # noqa: PLC0415

    observations = _recent_eval_sequence(conn, asset_id)
    verdict, llr = sprt_bernoulli(observations, p0=p0, p1=p1, alpha=alpha, beta=beta)
    if verdict is SprtVerdict.ACCEPT_H1:
        return True, (
            f"statistically significant regression: SPRT verdict={verdict.value} "
            f"(llr={llr:.3f}, n={len(observations)}, p0={p0}, p1={p1}, "
            f"alpha={alpha}, beta={beta})"
        )
    return False, verdict.value


def promote_asset(
    name: str,
    version: str,
    to_status: AssetStatus,
    actor: str,
    force: bool = False,
    db_path: Path | None = None,
    significance_gate: bool = False,
    sig_p0: float = _SIG_DEFAULT_P0,
    sig_p1: float = _SIG_DEFAULT_P1,
    sig_alpha: float = _SIG_DEFAULT_ALPHA,
    sig_beta: float = _SIG_DEFAULT_BETA,
) -> dict[str, Any]:
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        row = conn.execute(
            "SELECT * FROM assets WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if not row:
            raise AssetNotFoundError(f"Asset '{name}@{version}' not found.")

        current_status = AssetStatus(row["status"])
        if to_status not in _VALID_TRANSITIONS[current_status]:
            raise InvalidLifecycleTransitionError(
                f"Cannot transition '{name}@{version}' from "
                f"{current_status.value} to {to_status.value}."
            )

        # Policy gate — evaluated before the eval-score gate so that OPA
        # decisions are always logged regardless of the eval state.
        engine = get_policy_engine()
        inp = PolicyInput(
            action="promote",
            subject=PolicySubject(user=actor),
            resource=PolicyResource(
                kind="asset",
                ref=f"{name}@{version}",
                asset_type=row["asset_type"],
                eval_score=_latest_eval_score(conn, row["id"]),
                unsafe_skips=0,
            ),
        )
        decision = engine.evaluate(inp)
        AuditLog(AUDIT_LOG_PATH).append(
            event_type=(
                AuditEventType.POLICY_ALLOW if decision.allow else AuditEventType.POLICY_DENY
            ),
            actor=actor,
            resource_id=f"{name}@{version}",
            details={
                "decision_id": decision.decision_id,
                "reason": decision.reason,
                "action": "promote",
            },
        )
        if not decision.allow and not force:
            raise PromotionBlockedError(
                f"Policy denied promotion of '{name}@{version}': "
                f"{decision.reason} (decision_id={decision.decision_id})"
            )

        if (
            row["asset_type"] == AssetType.agent.value
            and to_status in _AGENT_GATE_STATUSES
            and not force
            and not _has_passing_eval(conn, row["id"])
        ):
            raise PromotionBlockedError(
                f"Agent '{name}@{version}' has no passing evaluation. "
                "Run `novafabric eval` first, or use --force to override."
            )

        # Opt-in statistical-significance gate (ADR-0080, gap-004). Only blocks on
        # a *statistically significant* regression over the recent eval sequence;
        # noise (ACCEPT_H0) and inconclusive evidence (CONTINUE) never block.
        if (
            significance_gate
            and row["asset_type"] == AssetType.agent.value
            and to_status in _AGENT_GATE_STATUSES
            and not force
        ):
            blocked, sig_reason = _significant_regression(
                conn, row["id"], p0=sig_p0, p1=sig_p1, alpha=sig_alpha, beta=sig_beta
            )
            if blocked:
                raise PromotionBlockedError(
                    f"Agent '{name}@{version}' blocked by significance gate: "
                    f"{sig_reason}. Use --force to override."
                )

        forced = int(force and row["asset_type"] == AssetType.agent.value)
        conn.execute(
            """
            UPDATE assets
            SET status = ?,
                promoted_at = ?,
                promoted_by = ?,
                forced_promotion = ?
            WHERE name = ? AND version = ?
            """,
            (to_status.value, _now(), actor, forced, name, version),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM assets WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        return _row_to_dict(updated)
    finally:
        conn.close()


def approve_asset(
    name: str,
    version: str,
    approver: str,
    note: str = "",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Record an approval sign-off for an asset pending approval.

    Returns a dict with keys: asset_name, asset_version, approver,
    approved_at, approval_id.
    Raises AssetNotFoundError if the asset doesn't exist.
    """
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        row = conn.execute(
            "SELECT id, status FROM assets WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if not row:
            raise AssetNotFoundError(f"Asset '{name}@{version}' not found.")

        if row["status"] != AssetStatus.pending_approval.value:
            raise InvalidLifecycleTransitionError(
                f"Cannot approve '{name}@{version}': asset must be in pending_approval status "
                f"(current: {row['status']})"
            )

        approval_id = str(uuid.uuid4())
        approved_at = _now()

        conn.execute(
            """
            INSERT INTO approvals
                (approval_id, asset_name, asset_version, approver, approved_at, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (approval_id, name, version, approver, approved_at, note),
        )
        conn.commit()
    finally:
        conn.close()

    # Audit write follows the committed DB record; an IO failure here leaves the DB authoritative.
    AuditLog(AUDIT_LOG_PATH).append(
        event_type=AuditEventType.APPROVE,
        actor=approver,
        resource_id=f"{name}@{version}",
        details={"approval_id": approval_id, "note": note},
    )

    return {
        "asset_name": name,
        "asset_version": version,
        "approver": approver,
        "approved_at": approved_at,
        "approval_id": approval_id,
    }


# ---------------------------------------------------------------------------
# C-1.4 — rollback_asset
# ---------------------------------------------------------------------------


def rollback_asset(
    name: str,
    target_version: str | None,
    actor: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Roll back an asset to its most recent previous production version.

    Archives the current production version and promotes the target version
    back to production in a single atomic DB transaction.

    Args:
        name: Asset name.
        target_version: Explicit version to restore.  If ``None``, the most
            recent version that was previously in ``production`` (determined
            from the audit log) is used.
        actor: Operator identity written to the audit trail.
        db_path: Override the registry DB path.

    Returns:
        dict with keys: archived_version, restored_version, rollback_id,
        skipped_archived.

    Raises:
        AssetNotFoundError: No asset with *name* exists, or *target_version*
            doesn't exist.
        RollbackError: No current production version; no previous production
            version found; or the target is already the current production
            version.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    conn = get_connection(db_path)
    init_schema(conn)
    try:
        # ------------------------------------------------------------------ #
        # 1. Find the current production version.                             #
        # ------------------------------------------------------------------ #
        prod_row = conn.execute(
            "SELECT * FROM assets WHERE name = ? AND status = ?",
            (name, AssetStatus.production.value),
        ).fetchone()
        if not prod_row:
            raise RollbackError(
                f"No current production version found for asset '{name}'. "
                "Use 'nova list --status production' to check the registry."
            )
        current_version = prod_row["version"]

        # ------------------------------------------------------------------ #
        # 2. Resolve the target version.                                      #
        # ------------------------------------------------------------------ #
        skipped_archived = False

        if target_version:
            # Explicit --to flag: verify the target exists.
            target_row = conn.execute(
                "SELECT * FROM assets WHERE name = ? AND version = ?",
                (name, target_version),
            ).fetchone()
            if not target_row:
                raise AssetNotFoundError(
                    f"Asset '{name}@{target_version}' not found in the registry."
                )
            if target_version == current_version:
                raise RollbackError(
                    f"'{name}@{target_version}' is already the current production version."
                )
        else:
            # Auto-detect: inspect audit log for the most recent version that
            # was previously promoted to any status (proxy for "was in production"),
            # excluding the current production version.
            # Strategy:
            #   1. Walk PROMOTE audit entries in reverse (newest first).
            #   2. Collect all versions of this asset that appear in the log,
            #      skipping the current production version.
            #   3. Take the first unique version found — this is the most
            #      recent "previous" version, regardless of its current status.
            #      A rollback can restore an archived version back to production.
            #   4. Fallback (no audit entries): query the DB directly for any
            #      other version ordered by promoted_at DESC.
            audit_log = AuditLog(AUDIT_LOG_PATH)
            promote_entries = audit_log.query(resource_id=None)
            # collect unique versions in reverse-chronological order
            seen: set[str] = set()
            prev_prod_versions: list[str] = []
            for entry in reversed(promote_entries):
                if not entry.resource_id.startswith(f"{name}@"):
                    continue
                if entry.event_type != AuditEventType.PROMOTE:
                    continue
                v = entry.resource_id.split("@", 1)[1]
                if v == current_version:
                    continue
                if v not in seen:
                    seen.add(v)
                    prev_prod_versions.append(v)

            # Fallback: audit log may be empty (new install, log rotated, etc.).
            # Query the DB for any other version with a promoted_at, ordered by
            # promoted_at DESC.  This includes archived versions.
            if not prev_prod_versions:
                other_rows = conn.execute(
                    """
                    SELECT version FROM assets
                    WHERE name = ? AND version != ?
                    ORDER BY COALESCE(promoted_at, created_at) DESC
                    """,
                    (name, current_version),
                ).fetchall()
                for r in other_rows:
                    prev_prod_versions.append(r["version"])

            if not prev_prod_versions:
                raise RollbackError(
                    f"No previous production version found for asset '{name}'. "
                    "Specify an explicit target with --to <version>."
                )

            # Pick the most recent candidate that actually exists in the DB.
            # If it was archived, note that in skipped_archived so the CLI can
            # display a warning to the operator.
            target_version = None
            for v in prev_prod_versions:
                target_row = conn.execute(
                    "SELECT * FROM assets WHERE name = ? AND version = ?",
                    (name, v),
                ).fetchone()
                if not target_row:
                    continue
                if target_row["status"] == AssetStatus.archived.value:
                    skipped_archived = True
                    _log.info(
                        "rollback: target version %s@%s is currently archived; "
                        "it will be promoted back to production",
                        name, v,
                    )
                target_version = v
                break

            if target_version is None:
                raise RollbackError(
                    f"No restorable previous version found for asset '{name}'. "
                    "Use --to <version> to specify an explicit target."
                )
            target_row = conn.execute(
                "SELECT * FROM assets WHERE name = ? AND version = ?",
                (name, target_version),
            ).fetchone()

        # ------------------------------------------------------------------ #
        # 3. Atomic transaction: archive current, promote target.             #
        # ------------------------------------------------------------------ #
        rollback_id = str(uuid.uuid4())
        rolled_back_at = _now()

        conn.execute("BEGIN EXCLUSIVE")
        try:
            conn.execute(
                """
                UPDATE assets
                SET status = ?,
                    promoted_at = ?,
                    promoted_by = ?
                WHERE name = ? AND version = ?
                """,
                (
                    AssetStatus.archived.value,
                    rolled_back_at,
                    actor,
                    name,
                    current_version,
                ),
            )
            conn.execute(
                """
                UPDATE assets
                SET status = ?,
                    promoted_at = ?,
                    promoted_by = ?,
                    forced_promotion = 0
                WHERE name = ? AND version = ?
                """,
                (
                    AssetStatus.production.value,
                    rolled_back_at,
                    actor,
                    name,
                    target_version,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    finally:
        conn.close()

    # ------------------------------------------------------------------ #
    # 4. Audit entry (outside the DB transaction; DB row is authoritative).#
    # ------------------------------------------------------------------ #
    AuditLog(AUDIT_LOG_PATH).append(
        event_type=AuditEventType.ROLLBACK,
        actor=actor,
        resource_id=f"{name}@{target_version}",
        details={
            "rollback_id": rollback_id,
            "archived_version": current_version,
            "restored_version": target_version,
            "rollback_reason": "manual-rollback",
            "skipped_archived": skipped_archived,
        },
    )

    return {
        "archived_version": current_version,
        "restored_version": target_version,
        "rollback_id": rollback_id,
        "skipped_archived": skipped_archived,
    }


# ---------------------------------------------------------------------------
# C-1.7 — unregister_asset
# ---------------------------------------------------------------------------

_UNREGISTER_BLOCKED_STATUSES = {
    AssetStatus.staging,
    AssetStatus.production,
    AssetStatus.pending_approval,
}


def unregister_asset(
    name: str,
    version: str,
    actor: str,
    force: bool = False,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Hard-delete an asset from the registry.

    Allowed without ``force``: development, validated, archived.
    Blocked without ``force``: staging, production, pending_approval.

    Deletes the assets row, eval_results rows (by asset_id), approvals rows
    (by name+version), and the synthetic ``reg:<name>@<version>`` lineage edges
    written by :func:`_record_declared_deps`.  Real consumed/depends_on edges
    are preserved — they record what actually happened in captured runs.

    Returns a snapshot of the deleted asset row.
    """
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        row = conn.execute(
            "SELECT * FROM assets WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if not row:
            raise AssetNotFoundError(f"Asset '{name}@{version}' not found.")

        current_status = AssetStatus(row["status"])
        if current_status in _UNREGISTER_BLOCKED_STATUSES and not force:
            raise UnregisterBlockedError(
                f"Cannot unregister '{name}@{version}': asset is in "
                f"{current_status.value} status. Archive it first, or use --force."
            )

        asset_id = row["id"]
        snapshot = _row_to_dict(row)

        conn.execute("BEGIN EXCLUSIVE")
        try:
            conn.execute("DELETE FROM eval_results WHERE asset_id = ?", (asset_id,))
            conn.execute(
                "DELETE FROM approvals WHERE asset_name = ? AND asset_version = ?",
                (name, version),
            )
            conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    _delete_synthetic_lineage_edges(name, version, db_path)

    AuditLog(AUDIT_LOG_PATH).append(
        event_type=AuditEventType.UNREGISTER,
        actor=actor,
        resource_id=f"{name}@{version}",
        details={
            "asset_id": asset_id,
            "status_at_deletion": current_status.value,
            "forced": force,
        },
    )
    return snapshot


def _delete_synthetic_lineage_edges(
    name: str, version: str, db_path: Path | None
) -> None:
    """Remove the synthetic reg: lineage edges written at registration time."""
    synthetic_run_id = f"reg:{name}@{version}"
    try:
        from novafabric.lineage._store import LineageStore

        store = LineageStore(db_path)
        store._conn.execute(
            "DELETE FROM lineage_edges WHERE payload LIKE ?",
            (f'%"capsule_run_id": "{synthetic_run_id}"%',),
        )
        store._conn.commit()
    except Exception:
        pass  # lineage store absence is not fatal


# ---------------------------------------------------------------------------
# C-1.6 — list_stale_assets
# ---------------------------------------------------------------------------


def list_stale_assets(
    stale_days: int = 30,
    asset_type: str | None = None,
    status: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return assets that have had no activity in the last *stale_days* days.

    "Activity" is defined as the most recent of:
    - ``promoted_at`` (last lifecycle promotion)
    - ``last_consumed_at`` from ``assets.jsonl`` records indexed in the
      lineage store (C-1.1); ``None`` if not recorded.
    - ``run_at`` from ``eval_results`` rows.

    Assets where ``last_activity_at < now - stale_days`` are considered stale.
    """
    from datetime import datetime, timedelta, timezone

    conn = get_connection(db_path)
    init_schema(conn)
    try:
        query = "SELECT * FROM assets WHERE 1=1"
        params: list[str] = []
        if asset_type:
            query += " AND asset_type = ?"
            params.append(asset_type)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        assets = [_row_to_dict(r) for r in rows]

        # Fetch the most recent eval_run_at per asset from eval_results.
        eval_run_map: dict[str, str] = {}
        eval_rows = conn.execute(
            "SELECT asset_id, MAX(run_at) AS last_eval FROM eval_results GROUP BY asset_id"
        ).fetchall()
        for r in eval_rows:
            eval_run_map[r["asset_id"]] = r["last_eval"]
    finally:
        conn.close()

    # Try to get last_consumed_at from the lineage store (C-1.1).
    # This is best-effort: if the lineage store is unavailable, we skip it.
    last_consumed_map: dict[str, str] = {}
    try:
        from novafabric.lineage._store import LineageStore

        lineage_store = LineageStore(db_path)
        lconn = lineage_store._conn
        consumed_rows = lconn.execute(
            """
            SELECT payload FROM lineage_edges
            WHERE edge_type = 'consumed'
            ORDER BY created_at DESC
            """
        ).fetchall()
        import json as _json

        for row in consumed_rows:
            try:
                payload = _json.loads(row["payload"])
                # source is the run node; target carries the asset_ref
                target = payload.get("target", {})
                asset_ref = target.get("asset_ref", "")
                if not asset_ref:
                    continue
                # asset_ref is "name@version"; extract name+version
                if "@" not in asset_ref:
                    continue
                name_part, ver_part = asset_ref.rsplit("@", 1)
                key = f"{name_part}@{ver_part}"
                created_at = payload.get("created_at", "")
                if created_at and key not in last_consumed_map:
                    last_consumed_map[key] = created_at
            except Exception:
                continue
    except Exception:
        pass

    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    stale: list[dict[str, Any]] = []

    for a in assets:
        # Gather candidate activity timestamps
        candidates: list[datetime] = []
        for ts_str in [a.get("promoted_at"), a.get("created_at")]:
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    candidates.append(dt)
                except ValueError:
                    pass

        # eval last run
        eval_ts = eval_run_map.get(a["id"])
        if eval_ts:
            try:
                dt = datetime.fromisoformat(eval_ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                candidates.append(dt)
            except ValueError:
                pass

        # last consumed (C-1.1 lineage)
        asset_key = f"{a['name']}@{a['version']}"
        consumed_ts = last_consumed_map.get(asset_key)
        if consumed_ts:
            try:
                dt = datetime.fromisoformat(consumed_ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                candidates.append(dt)
            except ValueError:
                pass

        if not candidates:
            continue

        last_activity = max(candidates)
        if last_activity < cutoff:
            days_stale = (datetime.now(timezone.utc) - last_activity).days
            row_out = dict(a)
            row_out["last_activity_at"] = last_activity.isoformat()
            row_out["days_stale"] = days_stale
            stale.append(row_out)

    return stale


# ---------------------------------------------------------------------------
# D-5 — Maker-checker dual-approval (ADR-0058)
# ---------------------------------------------------------------------------


def propose_promotion(
    name: str,
    version: str,
    to_status: AssetStatus,
    proposer: str,
    proposer_key_fp: str,
    proposer_sig: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Create a promotion proposal (maker step).

    Raises:
        AssetNotFoundError: asset does not exist.
        InvalidLifecycleTransitionError: transition not allowed from current status.
        SoDViolationError: an open proposal already exists for this asset.
    """
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        row = conn.execute(
            "SELECT * FROM assets WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if not row:
            raise AssetNotFoundError(f"Asset '{name}@{version}' not found.")

        current_status = AssetStatus(row["status"])
        if to_status not in _VALID_TRANSITIONS[current_status]:
            raise InvalidLifecycleTransitionError(
                f"Cannot transition '{name}@{version}' from "
                f"{current_status.value} to {to_status.value}."
            )

        existing = conn.execute(
            "SELECT proposal_id FROM promotion_proposals "
            "WHERE asset_name = ? AND asset_version = ? AND state = 'open'",
            (name, version),
        ).fetchone()
        if existing:
            raise SoDViolationError(
                f"An open proposal ({existing['proposal_id']}) already exists for "
                f"'{name}@{version}'. Reject it before opening a new one."
            )

        proposal_id = str(uuid.uuid4())
        proposed_at = _now()
        conn.execute(
            """
            INSERT INTO promotion_proposals
                (proposal_id, asset_name, asset_version, to_status,
                 proposer, proposer_key_fp, proposer_sig, proposed_at, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (proposal_id, name, version, to_status.value,
             proposer, proposer_key_fp, proposer_sig, proposed_at),
        )
        conn.commit()
    finally:
        conn.close()

    AuditLog(AUDIT_LOG_PATH).append(
        event_type=AuditEventType.PROMOTE_PROPOSE,
        actor=proposer,
        resource_id=f"{name}@{version}",
        details={
            "proposal_id": proposal_id,
            "to_status": to_status.value,
            "proposer_key_fp": proposer_key_fp,
        },
    )
    return {
        "proposal_id": proposal_id,
        "asset_name": name,
        "asset_version": version,
        "to_status": to_status.value,
        "proposer": proposer,
        "proposer_key_fp": proposer_key_fp,
        "proposed_at": proposed_at,
        "state": "open",
    }


def approve_promotion(
    name: str,
    version: str,
    approver: str,
    approver_key_fp: str,
    approver_sig: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Counter-sign an open promotion proposal (checker step) and execute the transition.

    Raises:
        AssetNotFoundError: no open proposal for this asset.
        SoDViolationError: approver identity or key fingerprint matches the proposer.
    """
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        proposal = conn.execute(
            "SELECT * FROM promotion_proposals "
            "WHERE asset_name = ? AND asset_version = ? AND state = 'open'",
            (name, version),
        ).fetchone()
        if not proposal:
            raise AssetNotFoundError(
                f"No open promotion proposal found for '{name}@{version}'."
            )

        if approver_key_fp == proposal["proposer_key_fp"]:
            raise SoDViolationError(
                "Approver key fingerprint matches proposer key fingerprint — "
                "SoD violation: maker and checker must use distinct keypairs."
            )
        if approver == proposal["proposer"]:
            raise SoDViolationError(
                f"Approver identity '{approver}' matches proposer — "
                "SoD violation: maker and checker must be different identities."
            )

        approved_at = _now()
        conn.execute(
            """
            UPDATE promotion_proposals
            SET state = 'approved', approver = ?, approver_key_fp = ?,
                approver_sig = ?, approved_at = ?
            WHERE proposal_id = ?
            """,
            (approver, approver_key_fp, approver_sig, approved_at,
             proposal["proposal_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    AuditLog(AUDIT_LOG_PATH).append(
        event_type=AuditEventType.PROMOTE_APPROVE,
        actor=approver,
        resource_id=f"{name}@{version}",
        details={
            "proposal_id": proposal["proposal_id"],
            "to_status": proposal["to_status"],
            "approver_key_fp": approver_key_fp,
        },
    )

    result = promote_asset(
        name,
        version,
        AssetStatus(proposal["to_status"]),
        actor=approver,
        db_path=db_path,
    )
    result["proposal_id"] = proposal["proposal_id"]
    result["approver"] = approver
    result["approved_at"] = approved_at
    return result
