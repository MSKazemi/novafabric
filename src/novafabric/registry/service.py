from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.adapters.git import get_head_sha
from novafabric.audit import AuditEventType, AuditLog, resolve_audit_log_path
from novafabric.policy import (
    NoopEngine,
    PolicyInput,
    PolicyResource,
    PolicySubject,
    get_policy_engine,
)
from novafabric.registry.signers import (
    SIGNOFF_SCHEME,
    SignerTrustList,
    TrustedSigner,
    UnverifiedProposalError,
    approval_payload,
    load_signer_trust_list,
    proposal_payload,
)
from novafabric.registry.store import (
    get_connection,
    init_schema,
    record_status_change,
    spec_digest,
)
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

#: Stages a single actor may not enter through `nova promote direct` without an explicit
#: `promote_direct` policy allow (ADR-0312 D1). Maker-checker (propose + approve) is the
#: default way in. A NoopEngine "allow" (no OPA configured) is not an explicit allow.
PROTECTED_STAGES: frozenset[AssetStatus] = frozenset({AssetStatus.production})


#: The one lifecycle status a new registration may declare. Every other status is
#: reached through ``nova promote`` / maker-checker / rollback, which run the gates
#: and write ``asset_status_history`` (ADR-0312).
REGISTRATION_STATUS: AssetStatus = AssetStatus.development


class DuplicateAssetError(Exception):
    pass


class VersionReuseError(DuplicateAssetError):
    """The name@version was in production before; its number can never be reused.

    A subclass of :class:`DuplicateAssetError` so every surface that already refuses a
    duplicate (exit 1, HTTP 409) refuses this the same way.
    """


class RegistrationStatusError(Exception):
    """A spec asks to be registered directly into a gated lifecycle status."""


class AssetNotFoundError(Exception):
    pass


class PromotionBlockedError(Exception):
    pass


class InvalidLifecycleTransitionError(Exception):
    pass


class DirectPromotionDeniedError(PromotionBlockedError):
    """Single-actor promotion into a protected stage lacks an explicit policy allow."""


class SoDViolationError(Exception):
    pass


class SelfApprovalError(SoDViolationError):
    """Maker and checker resolve to the same bound identity or key (ADR-0312)."""


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


def _require_registration_status(spec: BaseAssetSpec) -> None:
    """Refuse a spec that declares any status other than :data:`REGISTRATION_STATUS`.

    Registration is not a promotion: it runs no ``promote`` / ``promote_direct``
    policy decision, no eval gate and no maker-checker. A spec declaring
    ``status: production`` used to be inserted as-is, so ``nova register`` and both
    HTTP registration routes put an unreviewed asset straight into production with
    no history row and no promotion audit entry. Refusing (rather than silently
    registering it as ``development``) keeps the author's intent visible.
    """
    if spec.status is REGISTRATION_STATUS:
        return
    raise RegistrationStatusError(
        f"Cannot register '{spec.name}@{spec.version}' with status "
        f"'{spec.status.value}': a new asset always enters the registry as "
        f"'{REGISTRATION_STATUS.value}'. Remove the `status:` field (or set it to "
        f"{REGISTRATION_STATUS.value}), register, then move it with `nova promote` "
        "(maker-checker or a gated direct promotion), which runs the promotion gates "
        "and records the change (ADR-0312)."
    )


def _require_unused_production_version(
    conn: sqlite3.Connection, name: str, version: str
) -> None:
    """Refuse to register a name@version that the status history shows in production.

    A production version number is a reference other systems pin; registering new
    content under it after an ``unregister`` would silently change what that pin
    means, and used to let the new content borrow the old row's rollback evidence.
    """
    seen = conn.execute(
        "SELECT 1 FROM asset_status_history "
        "WHERE asset_name = ? AND version = ? AND to_status = ? LIMIT 1",
        (name, version, AssetStatus.production.value),
    ).fetchone()
    if seen:
        raise VersionReuseError(
            f"Asset '{name}@{version}' was in production before (registry status "
            "history); a version that has been in production can never be registered "
            "again. Register the new content as a new version."
        )


def register_asset(
    spec: BaseAssetSpec,
    spec_path: Path,
    db_path: Path | None = None,
    *,
    actor: str = "",
) -> dict[str, Any]:
    """Register *spec* as a new asset version, always in ``development``.

    Writes the assets row and a ``register`` row in ``asset_status_history`` bound to
    the new asset id and spec digest, in one transaction. *actor* is recorded as the
    history row's ``changed_by``.

    Raises:
        RegistrationStatusError: the spec declares a status other than development.
        VersionReuseError: name@version was in production before.
        DuplicateAssetError: name@version is already registered.
    """
    _require_registration_status(spec)
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
        _require_unused_production_version(conn, spec.name, spec.version)

        asset_id = str(uuid.uuid4())
        git_sha = get_head_sha()
        spec_json = spec.model_dump_json()
        created_at = _now()

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
                REGISTRATION_STATUS.value,
                spec_json,
                git_sha,
                created_at,
            ),
        )
        record_status_change(
            conn,
            asset_name=spec.name,
            version=spec.version,
            from_status=None,
            to_status=REGISTRATION_STATUS.value,
            changed_at=created_at,
            changed_by=actor,
            via="register",
            asset_id=asset_id,
            spec_sha256=spec_digest(spec_json),
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


#: ADR-0206 P2: total order for keyset paging of assets. ``id`` (the primary
#: key) breaks ``created_at`` ties, so equal timestamps page deterministically.
ASSET_KEYSET_ORDER = "created_at DESC, id DESC"


def list_assets_keyset(
    asset_type: str | None,
    status: str | None,
    *,
    limit: int,
    after: tuple[str | None, str] | None = None,
    with_total: bool = False,
    db_path: Path | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    """Return ``(rows, total)`` — one keyset page of list-view asset rows.

    ADR-0206 P2 (experimental). Rows are ordered ``created_at DESC, id DESC``
    and, when ``after = (created_at, id)`` names the last row of the previous
    page, only rows strictly after it are read — O(page), and stable when
    assets are registered or removed between pages. ``created_at`` is
    ``NOT NULL`` in the schema, so a ``None`` key admits nothing.

    At most ``limit`` rows are returned; callers ask for ``page + 1`` to learn
    whether another page exists. ``total`` is a ``COUNT(*)`` over the same
    filters only when ``with_total`` (the first page), else ``None``.
    ``spec_json`` is not selected.
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
        total: int | None = None
        if with_total:
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM assets {where}", params).fetchone()[0]
            )
        seek = ""
        seek_params: list[Any] = []
        if after is not None:
            created_at, asset_id = after
            if created_at is None:
                return [], total
            seek = " AND (created_at < ? OR (created_at = ? AND id < ?))"
            seek_params = [created_at, created_at, asset_id]
        rows = conn.execute(
            "SELECT id, name, version, asset_type, status, created_at, "
            f"promoted_at, git_commit_sha FROM assets {where}{seek} "
            f"ORDER BY {ASSET_KEYSET_ORDER} LIMIT ?",
            [*params, *seek_params, max(1, limit)],
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows], total
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
    observations: list[int] | None = None,
) -> tuple[bool, str]:
    """Decide whether the recent eval sequence shows a *significant* regression.

    Runs the Wald SPRT (ADR-0080) over the recent pass/fail sequence. Returns
    ``(blocked, reason)``: ``blocked`` is True only for ``ACCEPT_H1`` (a
    statistically significant regression). ``ACCEPT_H0`` (no regression) and
    ``CONTINUE`` (inconclusive — defer) never block, so a noisy single-run dip
    cannot fire the gate.
    """
    from novafabric.eval.significance import SprtVerdict, sprt_bernoulli  # noqa: PLC0415

    # Default source is the shipped eval_results sequence; callers may override with an
    # evidence-grade Score sequence read from a capsule's scores.jsonl (NF-007).
    if observations is None:
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
    sig_scores_path: Path | None = None,
    sig_scores_metric: str = "task_pass",
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

        _check_promote_gates(
            conn,
            row,
            to_status,
            actor=actor,
            force=force,
            significance_gate=significance_gate,
            sig_p0=sig_p0,
            sig_p1=sig_p1,
            sig_alpha=sig_alpha,
            sig_beta=sig_beta,
            sig_scores_path=sig_scores_path,
            sig_scores_metric=sig_scores_metric,
        )

        forced = int(force and row["asset_type"] == AssetType.agent.value)
        promoted_at = _now()
        conn.execute(
            """
            UPDATE assets
            SET status = ?,
                promoted_at = ?,
                promoted_by = ?,
                forced_promotion = ?
            WHERE name = ? AND version = ?
            """,
            (to_status.value, promoted_at, actor, forced, name, version),
        )
        record_status_change(
            conn,
            asset_name=name,
            version=version,
            from_status=row["status"],
            to_status=to_status.value,
            changed_at=promoted_at,
            changed_by=actor,
            via="promote",
            asset_id=row["id"],
            spec_sha256=spec_digest(row["spec_json"]),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM assets WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        return _row_to_dict(updated)
    finally:
        conn.close()


def _check_promote_gates(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    to_status: AssetStatus,
    *,
    actor: str,
    force: bool,
    significance_gate: bool = False,
    sig_p0: float = _SIG_DEFAULT_P0,
    sig_p1: float = _SIG_DEFAULT_P1,
    sig_alpha: float = _SIG_DEFAULT_ALPHA,
    sig_beta: float = _SIG_DEFAULT_BETA,
    sig_scores_path: Path | None = None,
    sig_scores_metric: str = "task_pass",
) -> None:
    """Every check :func:`promote_asset` runs before it changes a status.

    Lifecycle transition, the ``promote`` policy gate (audited), the agent eval gate
    and the opt-in significance gate. Shared with :func:`rollback_asset`, which runs
    the same checks for a target that was never in production. Mutates nothing.

    Raises:
        InvalidLifecycleTransitionError: *to_status* is not reachable from the row's status.
        PromotionBlockedError: a gate refused and *force* does not override it.
    """
    name, version = row["name"], row["version"]
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
    AuditLog(resolve_audit_log_path()).append(
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
    if not decision.allow:
        # ADR-0192 wired source. Emitted on the deny itself, not on the
        # raise, so a --force override is still alerted: an operator
        # bypassing a gate is precisely what someone should learn about.
        from novafabric.events.sources import (  # noqa: PLC0415
            emit_policy_violation_alert,
        )

        emit_policy_violation_alert(
            asset_id=f"{name}@{version}",
            decision_reason=decision.reason,
            stage="promote" if not force else "promote (forced)",
            source="nova promote",
        )

        # ADR-0137 lifecycle event, same placement rationale as the alert
        # above: emitted on the DENY, so a --force override is still
        # visible to downstream consumers. `forced` distinguishes them.
        from novafabric.events.lifecycle_sources import (  # noqa: PLC0415
            emit_policy_failed,
        )

        emit_policy_failed(
            asset_id=f"{name}@{version}",
            reason=decision.reason,
            decision_id=decision.decision_id,
            forced=bool(force),
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
        # Optionally source the sequence from an evidence-grade scores.jsonl
        # (NF-007). When no path is given the shipped eval_results path is used —
        # default behavior is byte-for-byte unchanged.
        observations: list[int] | None = None
        if sig_scores_path is not None:
            from novafabric.eval.scores import (  # noqa: PLC0415
                boolean_metric_outcomes,
                read_scores,
            )

            observations = boolean_metric_outcomes(
                read_scores(sig_scores_path), sig_scores_metric
            )
        blocked, sig_reason = _significant_regression(
            conn,
            row["id"],
            p0=sig_p0,
            p1=sig_p1,
            alpha=sig_alpha,
            beta=sig_beta,
            observations=observations,
        )
        if blocked:
            raise PromotionBlockedError(
                f"Agent '{name}@{version}' blocked by significance gate: "
                f"{sig_reason}. Use --force to override."
            )


def promote_asset_direct(
    name: str,
    version: str,
    to_status: AssetStatus,
    actor: str,
    force: bool = False,
    db_path: Path | None = None,
    *,
    policy_subject: str | None = None,
    **gate_kwargs: Any,
) -> dict[str, Any]:
    """Single-actor promotion, gated for protected stages (ADR-0312 D1).

    This is the one gate for every single-actor route into a protected stage:
    ``nova promote direct``, ``nova prompt promote``, the ``nova serve`` promote
    endpoints and the ``nova server`` route ``PUT /v0/assets/{id}/promote`` all call
    it, so they refuse and allow exactly the same promotions. :func:`rollback_asset`
    applies the same gate to any target without production history.

    For a target in :data:`PROTECTED_STAGES` the ``promote_direct`` policy action must
    return an explicit allow before :func:`promote_asset` runs. ``force`` does not bypass
    this gate (it only overrides the eval gates), and a :class:`NoopEngine` decision (no
    OPA configured) is not an explicit allow. Other stages are unchanged (ADR-0312 D1).

    *policy_subject* is the ``subject.user`` the policy sees, and defaults to *actor*.
    A caller that has an authenticated identity, such as the HTTP server's credential
    fingerprint, passes it here so an allow rule cannot be satisfied by a
    caller-chosen *actor* label. *actor* is still what ``promoted_by`` records.

    Raises:
        DirectPromotionDeniedError: protected target without an explicit policy allow.
        AssetNotFoundError / InvalidLifecycleTransitionError / PromotionBlockedError:
            as :func:`promote_asset`.
    """
    if to_status in PROTECTED_STAGES:
        conn = get_connection(db_path)
        init_schema(conn)
        try:
            row = conn.execute(
                "SELECT asset_type, status FROM assets WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise AssetNotFoundError(f"Asset '{name}@{version}' not found.")
        current_status = AssetStatus(row["status"])
        if to_status not in _VALID_TRANSITIONS[current_status]:
            # An impossible move is reported as such, not as a policy refusal.
            raise InvalidLifecycleTransitionError(
                f"Cannot transition '{name}@{version}' from "
                f"{current_status.value} to {to_status.value}."
            )
        _require_direct_allow(
            name,
            version,
            to_status,
            asset_type=row["asset_type"],
            actor=actor,
            force=force,
            policy_subject=policy_subject,
        )
    return promote_asset(
        name, version, to_status, actor=actor, force=force, db_path=db_path, **gate_kwargs
    )


def _require_direct_allow(
    name: str,
    version: str,
    to_status: AssetStatus,
    *,
    asset_type: str,
    actor: str,
    force: bool,
    policy_subject: str | None,
    via: str = "promote_direct",
) -> None:
    """The ADR-0312 D1 gate: an explicit ``promote_direct`` allow, or refuse.

    Evaluated for every single-actor move into a protected stage — ``nova promote
    direct``, the HTTP promote routes, ``nova prompt promote`` and a rollback whose
    target was never in production. A :class:`NoopEngine` result is not an allow,
    and *force* never bypasses it. Every decision is audited.

    Raises:
        DirectPromotionDeniedError: no explicit allow.
    """
    subject = policy_subject or actor
    engine = get_policy_engine()
    decision = engine.evaluate(
        PolicyInput(
            action="promote_direct",
            subject=PolicySubject(user=subject),
            resource=PolicyResource(
                kind="asset",
                ref=f"{name}@{version}",
                asset_type=asset_type,
                to_status=to_status.value,
            ),
            context={"protected_stage": True, "via": via},
        )
    )
    explicit_allow = decision.allow and not isinstance(engine, NoopEngine)
    details: dict[str, Any] = {
        "decision_id": decision.decision_id,
        "reason": decision.reason,
        "action": "promote_direct",
        "to_status": to_status.value,
        "forced": bool(force),
        "via": via,
    }
    if policy_subject:
        details["actor_label"] = actor
    AuditLog(resolve_audit_log_path()).append(
        event_type=(
            AuditEventType.POLICY_ALLOW if explicit_allow else AuditEventType.POLICY_DENY
        ),
        actor=subject,
        resource_id=f"{name}@{version}",
        details=details,
    )
    if not explicit_allow:
        why = (
            "no policy engine is configured (opa not on PATH), so there is no "
            "explicit allow"
            if isinstance(engine, NoopEngine)
            else f"policy denied: {decision.reason} (decision_id={decision.decision_id})"
        )
        raise DirectPromotionDeniedError(
            f"Direct (single-actor) promotion of '{name}@{version}' into protected "
            f"stage '{to_status.value}' refused: {why}. Use `nova promote propose` + "
            "`nova promote approve` (maker-checker), or load a policy that allows "
            "action 'promote_direct' (ADR-0312). --force does not bypass this gate."
        )


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
    AuditLog(resolve_audit_log_path()).append(
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


def production_history_versions(
    name: str, db_path: Path | None = None
) -> list[str]:
    """Registered versions of *name* with production history of their own, newest first.

    Read from the append-only ``asset_status_history`` table (each version once).
    Only a history row bound to the version's *current* registration counts (see
    :func:`_history_binds`), so a version number that was unregistered and
    registered again with new content is not listed. This is the evidence
    :func:`rollback_asset` needs to restore a version without the ADR-0312
    ``promote_direct`` gate.
    """
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        return _production_history(conn, name)
    finally:
        conn.close()


def _parse_utc(ts: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _history_binds(history: sqlite3.Row, asset: sqlite3.Row) -> bool:
    """Whether a status-history row is evidence about this exact registration.

    A bound row must name the asset row's id **and** the SHA-256 of its current
    ``spec_json``: name@version alone is reusable (unregister, register again), and
    keying evidence by it let new content borrow the old version's production
    history. A legacy row (written before the binding columns, ``asset_id`` NULL)
    counts only if it was written no earlier than this registration: name@version is
    unique in ``assets``, so such a row cannot be about an earlier, since-deleted row.
    An unparseable timestamp is not evidence.
    """
    if history["asset_id"] is not None:
        return bool(
            history["asset_id"] == asset["id"]
            and history["spec_sha256"] == spec_digest(asset["spec_json"])
        )
    changed = _parse_utc(str(history["changed_at"]))
    created = _parse_utc(str(asset["created_at"]))
    return changed is not None and created is not None and changed >= created


def _production_history(conn: sqlite3.Connection, name: str) -> list[str]:
    assets = {
        str(a["version"]): a
        for a in conn.execute(
            "SELECT id, version, spec_json, created_at FROM assets WHERE name = ?", (name,)
        )
    }
    versions: list[str] = []
    for h in conn.execute(
        "SELECT version, asset_id, spec_sha256, changed_at FROM asset_status_history "
        "WHERE asset_name = ? AND to_status = ? ORDER BY id DESC",
        (name, AssetStatus.production.value),
    ):
        version = str(h["version"])
        asset = assets.get(version)
        if version in versions or asset is None or not _history_binds(h, asset):
            continue
        versions.append(version)
    return versions


def rollback_asset(
    name: str,
    target_version: str | None,
    actor: str,
    db_path: Path | None = None,
    *,
    policy_subject: str | None = None,
) -> dict[str, Any]:
    """Roll back an asset to a version that was previously in production.

    Archives the current production version and puts the target in production in a
    single atomic DB transaction.

    **Gate (ADR-0312 D1).** A rollback restores, without a policy decision, only a
    version the registry's status history (``asset_status_history``) shows was in
    ``production`` before — that is what a rollback is for. Any other explicit
    target is a single-actor move into a protected stage: it must pass the same
    ``promote_direct`` gate as ``nova promote direct`` (an explicit policy allow; a
    NoopEngine is not one) and every check :func:`promote_asset` runs, including the
    lifecycle rules. Without this, ``promote --to staging`` followed by a rollback
    put an unreviewed version into production.

    Args:
        name: Asset name.
        target_version: Explicit version to restore. If ``None``, the most recent
            version with production history, other than the current one, is used;
            no other version is ever chosen automatically.
        actor: Operator identity written to the audit trail and ``promoted_by``.
        db_path: Override the registry DB path.
        policy_subject: The authenticated identity the policy sees for a gated
            target (``serve:<fingerprint>`` over HTTP). Defaults to *actor*.

    Returns:
        dict with keys: archived_version, restored_version, rollback_id,
        skipped_archived, gate (``previously_production`` or ``promote_direct``).

    Raises:
        AssetNotFoundError: *target_version* doesn't exist.
        RollbackError: No current production version; no previous production
            version; or the target is already the current production version.
        DirectPromotionDeniedError: a target with no production history lacks an
            explicit ``promote_direct`` allow.
        InvalidLifecycleTransitionError / PromotionBlockedError: a target with no
            production history fails the lifecycle or promote gates.
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
        previously_production = _production_history(conn, name)

        # ------------------------------------------------------------------ #
        # 2. Resolve the target version.                                      #
        # ------------------------------------------------------------------ #
        if target_version:
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
            target_row = None
            for v in previously_production:
                if v == current_version:
                    continue
                target_row = conn.execute(
                    "SELECT * FROM assets WHERE name = ? AND version = ?",
                    (name, v),
                ).fetchone()
                if target_row is not None:
                    break
            if target_row is None:
                raise RollbackError(
                    f"No previous production version found for asset '{name}' in the "
                    "registry's status history. Specify an explicit target with "
                    "--to <version>; a version that was never in production needs a "
                    "'promote_direct' policy allow (ADR-0312) or maker-checker."
                )
            target_version = str(target_row["version"])

        skipped_archived = target_row["status"] == AssetStatus.archived.value

        # ------------------------------------------------------------------ #
        # 3. Gate: restore ungated only with production history (ADR-0312). #
        # ------------------------------------------------------------------ #
        if target_version in previously_production:
            gate = "previously_production"
            if skipped_archived:
                _log.info(
                    "rollback: target version %s@%s is currently archived; "
                    "it will be promoted back to production",
                    name, target_version,
                )
        else:
            gate = "promote_direct"
            _require_direct_allow(
                name,
                target_version,
                AssetStatus.production,
                asset_type=target_row["asset_type"],
                actor=actor,
                force=False,
                policy_subject=policy_subject,
                via="rollback",
            )
            _check_promote_gates(
                conn, target_row, AssetStatus.production, actor=actor, force=False
            )

        # ------------------------------------------------------------------ #
        # 4. Atomic transaction: archive current, promote target.             #
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
            record_status_change(
                conn,
                asset_name=name,
                version=current_version,
                from_status=AssetStatus.production.value,
                to_status=AssetStatus.archived.value,
                changed_at=rolled_back_at,
                changed_by=actor,
                via="rollback",
                asset_id=prod_row["id"],
                spec_sha256=spec_digest(prod_row["spec_json"]),
            )
            record_status_change(
                conn,
                asset_name=name,
                version=target_version,
                from_status=target_row["status"],
                to_status=AssetStatus.production.value,
                changed_at=rolled_back_at,
                changed_by=actor,
                via=f"rollback:{gate}",
                asset_id=target_row["id"],
                spec_sha256=spec_digest(target_row["spec_json"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    finally:
        conn.close()

    # ------------------------------------------------------------------ #
    # 5. Audit entry (outside the DB transaction; DB row is authoritative).#
    # ------------------------------------------------------------------ #
    AuditLog(resolve_audit_log_path()).append(
        event_type=AuditEventType.ROLLBACK,
        actor=actor,
        resource_id=f"{name}@{target_version}",
        details={
            "rollback_id": rollback_id,
            "archived_version": current_version,
            "restored_version": target_version,
            "rollback_reason": "manual-rollback",
            "skipped_archived": skipped_archived,
            "gate": gate,
        },
    )

    return {
        "archived_version": current_version,
        "restored_version": target_version,
        "rollback_id": rollback_id,
        "skipped_archived": skipped_archived,
        "gate": gate,
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

    AuditLog(resolve_audit_log_path()).append(
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


def new_proposal_id() -> str:
    """A fresh proposal id. The maker signs over it, so the client chooses it (ADR-0312)."""
    return str(uuid.uuid4())


def _require_proposal_id(proposal_id: str) -> None:
    try:
        canonical = str(uuid.UUID(proposal_id))
    except (ValueError, TypeError, AttributeError):
        canonical = ""
    if canonical != proposal_id:
        raise ValueError(f"proposal_id must be a canonical UUID string, got {proposal_id!r}")


def proposal_verification(row: sqlite3.Row | dict[str, Any]) -> str:
    """``"verified"`` for a row written under ADR-0312, else ``"unverified"`` (legacy)."""
    keys = row.keys()
    scheme = row["signoff_scheme"] if "signoff_scheme" in keys else None
    return "verified" if scheme else "unverified"


def get_open_proposal(
    name: str, version: str, db_path: Path | None = None
) -> dict[str, Any] | None:
    """Return the open promotion proposal for ``name@version``, or ``None``.

    The dict carries every stored column plus ``verification`` (``"verified"`` /
    ``"unverified"``); a legacy row stored before ADR-0312 reads as ``"unverified"``.
    The approver needs ``proposal_id``, ``to_status`` and ``proposer_sig`` to build the
    payload it signs (:func:`novafabric.registry.signers.approval_payload`).
    """
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        row = conn.execute(
            "SELECT * FROM promotion_proposals "
            "WHERE asset_name = ? AND asset_version = ? AND state = 'open'",
            (name, version),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    result: dict[str, Any] = {k: row[k] for k in row.keys()}
    result["verification"] = proposal_verification(row)
    return result


def propose_promotion(
    name: str,
    version: str,
    to_status: AssetStatus,
    proposer: str,
    proposer_key_fp: str,
    proposer_sig: str,
    db_path: Path | None = None,
    *,
    proposal_id: str,
    trust_list: SignerTrustList | None = None,
) -> dict[str, Any]:
    """Create a promotion proposal (maker step), verifying the maker's signature.

    The signature must verify over
    :func:`~novafabric.registry.signers.proposal_payload` for *proposal_id* with the
    public key the signer trust list binds to *proposer*, and that identity must hold the
    ``proposer`` role (ADR-0312). An open proposal stored before ADR-0312 (unverified)
    is superseded rather than blocking the asset forever.

    Raises:
        AssetNotFoundError: asset does not exist.
        InvalidLifecycleTransitionError: transition not allowed from current status.
        SignerVerificationError: trust list missing/malformed, key unbound or bound to
            another identity, role missing, or signature invalid.
        SoDViolationError: a verified open proposal already exists, or the id was used.
        ValueError: *proposal_id* is not a canonical UUID.
    """
    _require_proposal_id(proposal_id)
    trust = trust_list or load_signer_trust_list()
    conn = get_connection(db_path)
    init_schema(conn)
    superseded: str | None = None
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

        maker: TrustedSigner = trust.verify(
            claimed_identity=proposer,
            key_fp=proposer_key_fp,
            role="proposer",
            payload=proposal_payload(
                proposal_id=proposal_id, name=name, version=version,
                to_status=to_status.value,
            ),
            signature=proposer_sig,
            party="proposer",
        )

        existing = conn.execute(
            "SELECT * FROM promotion_proposals "
            "WHERE asset_name = ? AND asset_version = ? AND state = 'open'",
            (name, version),
        ).fetchone()
        if existing and proposal_verification(existing) == "verified":
            raise SoDViolationError(
                f"An open proposal ({existing['proposal_id']}) already exists for "
                f"'{name}@{version}'. Approve it before opening a new one."
            )
        if existing:
            # Legacy, unverified: it can never be approved (approve_promotion refuses it),
            # so leaving it open would block the asset forever.
            superseded = str(existing["proposal_id"])
            conn.execute(
                "UPDATE promotion_proposals SET state = 'superseded' "
                "WHERE proposal_id = ? AND state = 'open'",
                (superseded,),
            )

        proposed_at = _now()
        try:
            conn.execute(
                """
                INSERT INTO promotion_proposals
                    (proposal_id, asset_name, asset_version, to_status,
                     proposer, proposer_key_fp, proposer_sig, proposed_at, state,
                     signoff_scheme)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (proposal_id, name, version, to_status.value,
                 maker.identity, maker.fingerprint, proposer_sig, proposed_at,
                 SIGNOFF_SCHEME),
            )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise SoDViolationError(
                f"Proposal id {proposal_id} has already been used; sign a new proposal."
            ) from exc
        conn.commit()
    finally:
        conn.close()

    details: dict[str, Any] = {
        "proposal_id": proposal_id,
        "to_status": to_status.value,
        "proposer_key_fp": maker.fingerprint,
        "signoff_scheme": SIGNOFF_SCHEME,
    }
    if superseded:
        details["superseded_unverified_proposal"] = superseded
    AuditLog(resolve_audit_log_path()).append(
        event_type=AuditEventType.PROMOTE_PROPOSE,
        actor=maker.identity,
        resource_id=f"{name}@{version}",
        details=details,
    )
    return {
        "proposal_id": proposal_id,
        "asset_name": name,
        "asset_version": version,
        "to_status": to_status.value,
        "proposer": maker.identity,
        "proposer_key_fp": maker.fingerprint,
        "proposed_at": proposed_at,
        "state": "open",
        "verification": "verified",
        "superseded": superseded,
    }


def approve_promotion(
    name: str,
    version: str,
    approver: str,
    approver_key_fp: str,
    approver_sig: str,
    db_path: Path | None = None,
    *,
    trust_list: SignerTrustList | None = None,
) -> dict[str, Any]:
    """Counter-sign an open promotion proposal (checker step) and execute the transition.

    Both signatures are verified against the signer trust list (ADR-0312): the stored
    maker signature is re-verified (so an edited proposal, or a maker since removed from
    the list, is refused) and the checker's signature must verify over
    :func:`~novafabric.registry.signers.approval_payload` for this exact proposal. SoD
    compares the identities the trust list binds the two keys to.

    Raises:
        AssetNotFoundError: no open proposal for this asset.
        UnverifiedProposalError: the open proposal predates ADR-0312.
        SignerVerificationError: either signature or binding fails (``exc.party`` says
            which side).
        SelfApprovalError: maker and checker are the same bound identity or key.
        SoDViolationError: the proposal stopped being open while approving.
    """
    trust = trust_list or load_signer_trust_list()
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
        proposal_id = str(proposal["proposal_id"])
        to_status = str(proposal["to_status"])
        if proposal_verification(proposal) != "verified":
            raise UnverifiedProposalError(
                f"Open proposal {proposal_id} for '{name}@{version}' is unverified: it was "
                "stored before maker-checker signatures were verified (ADR-0312) and cannot "
                "be approved. Open a new one with `nova promote propose` (it supersedes "
                "the unverified proposal).",
                party="proposer",
            )

        maker = trust.verify(
            claimed_identity=str(proposal["proposer"]),
            key_fp=str(proposal["proposer_key_fp"]),
            role="proposer",
            payload=proposal_payload(
                proposal_id=proposal_id, name=name, version=version, to_status=to_status
            ),
            signature=str(proposal["proposer_sig"]),
            party="proposer",
        )
        checker = trust.verify(
            claimed_identity=approver,
            key_fp=approver_key_fp,
            role="approver",
            payload=approval_payload(
                proposal_id=proposal_id, name=name, version=version, to_status=to_status,
                proposer_sig=str(proposal["proposer_sig"]),
            ),
            signature=approver_sig,
            party="approver",
        )
        if checker.identity == maker.identity or checker.fingerprint == maker.fingerprint:
            raise SelfApprovalError(
                f"SoD violation: maker and checker resolve to the same bound identity "
                f"{maker.identity!r} — they must be different people with distinct "
                "keypairs bound in the signer trust list."
            )

        approved_at = _now()
        cur = conn.execute(
            """
            UPDATE promotion_proposals
            SET state = 'approved', approver = ?, approver_key_fp = ?,
                approver_sig = ?, approved_at = ?
            WHERE proposal_id = ? AND state = 'open'
            """,
            (checker.identity, checker.fingerprint, approver_sig, approved_at, proposal_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise SoDViolationError(
                f"Proposal {proposal_id} is no longer open; nothing was approved."
            )
        conn.commit()
    finally:
        conn.close()

    AuditLog(resolve_audit_log_path()).append(
        event_type=AuditEventType.PROMOTE_APPROVE,
        actor=checker.identity,
        resource_id=f"{name}@{version}",
        details={
            "proposal_id": proposal_id,
            "to_status": to_status,
            "approver_key_fp": checker.fingerprint,
            "proposer": maker.identity,
            "proposer_key_fp": maker.fingerprint,
            "signoff_scheme": SIGNOFF_SCHEME,
        },
    )

    result = promote_asset(
        name,
        version,
        AssetStatus(to_status),
        actor=checker.identity,
        db_path=db_path,
    )
    result["proposal_id"] = proposal_id
    result["approver"] = checker.identity
    result["approved_at"] = approved_at
    result["verification"] = "verified"
    return result
