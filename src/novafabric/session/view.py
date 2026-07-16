"""Read-only session assembly: resolve members and aggregate stats (ADR-0122).

Resolution never fails the whole command: a member whose ``capsule_ref``
cannot be located is reported ``missing``; one whose ``capsule.yaml`` no
longer matches the recorded content digest is reported ``tampered`` (the
session is never silently repaired). Everything here reads capsules; nothing
writes them (one capsule, one writer).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from novafabric.session.manifest import (
    MemberRun,
    SessionManifest,
    capsule_manifest_digest,
    parse_capsule_ref,
    session_manifest_path,
)

logger = logging.getLogger(__name__)

MemberStatus = Literal["ok", "missing", "tampered"]


class ResolvedMember(BaseModel):
    """One session member after locating (or failing to locate) its capsule."""

    member: MemberRun
    status: MemberStatus
    capsule_dir: str | None = None
    #: The member run's own outcome (``success``/``failure``/…), when readable.
    run_status: str | None = None
    duration_ms: int | None = None
    finished_at: str | None = None
    total_tokens: int | None = None
    #: Recorded ``nova.cost`` totals from the member's model calls, by currency.
    cost_by_currency: dict[str, float] | None = None


class SessionStats(BaseModel):
    """Aggregate view over the resolved members of one session."""

    turns: int
    resolved: int
    missing: int
    tampered: int
    total_duration_ms: int | None = None
    first_started_at: str | None = None
    last_finished_at: str | None = None
    total_tokens: int | None = None
    cost_by_currency: dict[str, float] | None = None


def _candidate_dirs(
    member: MemberRun, session_dir: Path, capsule_base: Path | None
) -> tuple[list[Path], str]:
    path_prefix, digest = parse_capsule_ref(member.capsule_ref)
    candidates: list[Path] = []
    if path_prefix is not None:
        prefix = Path(path_prefix)
        candidates.append(prefix if prefix.is_absolute() else session_dir / prefix)
    if capsule_base is not None:
        candidates.append(capsule_base / member.run_id)
    return candidates, digest


def _member_cost(capsule_dir: Path) -> dict[str, float]:
    """Sum the recorded ``nova.cost`` blocks in the member's model calls.

    Read-only over model-calls.jsonl; amounts were written at capture time
    (ADR-0066) and are never recomputed here. Unreadable lines are skipped.
    """
    totals: dict[str, float] = {}
    calls_file = capsule_dir / "model-calls.jsonl"
    if not calls_file.is_file():
        return totals
    import json

    for line in calls_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        cost_block = record.get("nova.cost") if isinstance(record, dict) else None
        if not isinstance(cost_block, dict):
            continue
        amount = cost_block.get("amount")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            continue
        currency = cost_block.get("currency")
        key = currency if isinstance(currency, str) and currency else "USD"
        totals[key] = totals.get(key, 0.0) + float(amount)
    return totals


def _resolve_one(
    member: MemberRun, session_dir: Path, capsule_base: Path | None
) -> ResolvedMember:
    candidates, digest = _candidate_dirs(member, session_dir, capsule_base)
    found: Path | None = None
    matched = False
    for candidate in candidates:
        if not (candidate / "capsule.yaml").is_file():
            continue
        found = candidate
        if capsule_manifest_digest(candidate) == digest:
            matched = True
            break
    if found is None:
        return ResolvedMember(member=member, status="missing")
    if not matched:
        return ResolvedMember(
            member=member, status="tampered", capsule_dir=str(found)
        )

    resolved = ResolvedMember(member=member, status="ok", capsule_dir=str(found))
    try:
        capsule: dict[str, Any] = yaml.safe_load(
            (found / "capsule.yaml").read_text(encoding="utf-8")
        )
    except yaml.YAMLError:  # pragma: no cover - digest already matched
        return resolved
    if isinstance(capsule, dict):
        resolved.run_status = capsule.get("status")
        duration = capsule.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool):
            resolved.duration_ms = duration
        finished = capsule.get("finished_at")
        if isinstance(finished, str):
            resolved.finished_at = finished
        usage = capsule.get("usage_totals")
        if isinstance(usage, dict):
            tokens = usage.get("total_tokens")
            if isinstance(tokens, int) and not isinstance(tokens, bool):
                resolved.total_tokens = tokens
    cost = _member_cost(found)
    if cost:
        resolved.cost_by_currency = cost
    return resolved


def resolve_members(
    manifest: SessionManifest,
    root: Path | None = None,
    capsule_base: Path | None = None,
) -> list[ResolvedMember]:
    """Locate every member capsule of *manifest*, in sequence order.

    Each ``capsule_ref`` path prefix is resolved relative to the session's
    manifest directory; a bare-digest ref (or a moved capsule) is also looked
    up as ``<capsule_base>/<run_id>``. Missing/tampered members are reported,
    never raised.
    """
    session_dir = session_manifest_path(manifest.session_id, root).parent
    return [
        _resolve_one(member, session_dir, capsule_base)
        for member in sorted(manifest.member_runs, key=lambda m: m.sequence)
    ]


def session_stats(resolved: list[ResolvedMember]) -> SessionStats:
    """Aggregate turns / duration / tokens / recorded cost over one session.

    Sums are over resolvable (``ok``) members only; absent per-member values
    are skipped, never counted as zero. ``total_duration_ms`` is the sum of
    member wall-clock durations (turns of a session do not overlap by
    construction, but gaps between turns are not counted).
    """
    ok = [r for r in resolved if r.status == "ok"]
    durations = [r.duration_ms for r in ok if r.duration_ms is not None]
    tokens = [r.total_tokens for r in ok if r.total_tokens is not None]
    finishes = [r.finished_at for r in ok if r.finished_at is not None]
    cost: dict[str, float] = {}
    for r in ok:
        for currency, amount in (r.cost_by_currency or {}).items():
            cost[currency] = cost.get(currency, 0.0) + amount
    starts = [r.member.started_at for r in resolved]
    return SessionStats(
        turns=len(resolved),
        resolved=len(ok),
        missing=sum(1 for r in resolved if r.status == "missing"),
        tampered=sum(1 for r in resolved if r.status == "tampered"),
        total_duration_ms=sum(durations) if durations else None,
        first_started_at=min(starts) if starts else None,
        last_finished_at=max(finishes) if finishes else None,
        total_tokens=sum(tokens) if tokens else None,
        cost_by_currency=cost or None,
    )
