"""Forensics-timeline dashboard read surface (ADR-0155 D1, ADR-0183 pattern).

``GET /api/runs/{run_id}/forensics-timeline`` builds a deterministic
:class:`~novafabric.forensics.timeline.ForensicsTimeline` from the run's own
sealed capsule and returns it via the pure
:func:`~novafabric.forensics.timeline.merge_timeline` — the same merge core
``nova forensics timeline`` uses, given an evidence document instead of a
file path.

**What evidence this endpoint reconstructs (honest scope).** The
``forensics/timeline.py`` module docstring is explicit that ``merge_timeline``
is "the pure deterministic merge core" and that "the incident → session →
lineage *collector* that gathers those records … is a documented follow-on
slice" — no such collector exists anywhere in the capture path yet. This
endpoint is that follow-on's first, narrowest real slice: it reconstructs
only what a run's own sealed capsule already carries —

- two ``run``-kind lifecycle events (``created_at`` / ``finished_at``);
- one ``model-call`` event per ``model-calls.jsonl`` record with a
  ``started_at`` timestamp;
- one ``tool-call`` event per ``tool-calls.jsonl`` record with a
  ``started_at`` timestamp.

Records missing a usable timestamp are **gaps**, never fabricated events
(fail-open, per the module's own contract). Lineage evidence has no
collector at all yet, so a lineage gap is always reported — this endpoint
never claims lineage coverage it does not have. Each source is capped at
:data:`MAX_EVENTS_PER_SOURCE`; anything beyond the cap is a gap, not a
silent drop.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from novafabric.forensics.timeline import TimelineEvent, merge_timeline
from novafabric.serve.capsule_loader import load_capsule_manifest, load_jsonl
from novafabric.serve.http_cache import conditional_json

#: Bounded read per evidence source — a pathological trace does not turn
#: this endpoint into an unbounded scan (mirrors the read-only guarantee
#: every other ADR-0183 router router carries).
MAX_EVENTS_PER_SOURCE = 1000

#: (kind, jsonl filename, the record field to label the event with).
_CALL_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("model-call", "model-calls.jsonl", "model"),
    ("tool-call", "tool-calls.jsonl", "tool_name"),
)


def _lifecycle_events(
    run_id: str, manifest: dict[str, Any]
) -> tuple[list[TimelineEvent], list[str]]:
    events: list[TimelineEvent] = []
    gaps: list[str] = []
    created_at = manifest.get("created_at")
    if isinstance(created_at, str) and created_at:
        events.append(
            TimelineEvent(
                ts=created_at,
                source_capsule=run_id,
                seq=0,
                kind="run",
                detail=f"run created (status={manifest.get('status')})",
            )
        )
    else:
        gaps.append(f"run:{run_id}: manifest has no parsable created_at")

    finished_at = manifest.get("finished_at")
    if isinstance(finished_at, str) and finished_at:
        events.append(
            TimelineEvent(
                ts=finished_at,
                source_capsule=run_id,
                seq=1,
                kind="run",
                detail=f"run finished (exit_code={manifest.get('exit_code')})",
            )
        )
    return events, gaps


def _call_events(
    run_id: str, cdir: Path, kind: str, filename: str, label_field: str, seq_start: int
) -> tuple[list[TimelineEvent], list[str], int]:
    events: list[TimelineEvent] = []
    gaps: list[str] = []
    records = load_jsonl(cdir, filename)
    seq = seq_start
    omitted_missing_ts = 0
    for record in records[:MAX_EVENTS_PER_SOURCE]:
        ts = record.get("started_at")
        if not isinstance(ts, str) or not ts:
            omitted_missing_ts += 1
            continue
        label = record.get(label_field) or kind
        call_id = record.get("call_id")
        detail = f"{label} ({call_id})" if call_id else str(label)
        events.append(
            TimelineEvent(ts=ts, source_capsule=run_id, seq=seq, kind=kind, detail=detail)
        )
        seq += 1
    if len(records) > MAX_EVENTS_PER_SOURCE:
        gaps.append(
            f"{kind}: {len(records) - MAX_EVENTS_PER_SOURCE} further record(s) omitted "
            f"beyond the {MAX_EVENTS_PER_SOURCE}-record bounded-read cap"
        )
    if omitted_missing_ts:
        gaps.append(f"{kind}: {omitted_missing_ts} record(s) missing a usable started_at timestamp")
    return events, gaps, seq


def _collect_evidence(
    run_id: str, cdir: Path, manifest: dict[str, Any]
) -> tuple[list[TimelineEvent], list[str]]:
    events, gaps = _lifecycle_events(run_id, manifest)
    seq = len(events)
    for kind, filename, label_field in _CALL_SOURCES:
        source_events, source_gaps, seq = _call_events(
            run_id, cdir, kind, filename, label_field, seq
        )
        events.extend(source_events)
        gaps.extend(source_gaps)
    # No lineage collector exists yet (see module docstring) — always an
    # honest gap, never fabricated coverage.
    gaps.append(
        f"lineage-edge:{run_id}: lineage evidence collection is not wired for this "
        "endpoint yet (ADR-0155 follow-on)"
    )
    return events, gaps


def build_forensics_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: Path,
    resolve_capsule: Callable[[str, Path], Path],
) -> APIRouter:
    """Router for ``GET /api/runs/{run_id}/forensics-timeline``.

    *resolve_capsule* is injected rather than imported so this module does
    not depend on ``serve.app`` (which imports this one) — the same pattern
    ``trust_surfaces.py`` uses.
    """
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["forensics"])

    @router.get("/api/runs/{run_id}/forensics-timeline")
    async def get_forensics_timeline(run_id: str, request: Request) -> Response:
        cdir = resolve_capsule(run_id, capsule_dir)
        manifest = load_capsule_manifest(cdir)
        records, gaps = _collect_evidence(run_id, cdir, manifest)
        timeline = merge_timeline(run_id, records, gaps=gaps)
        # A sealed capsule's reconstructed timeline is immutable, so a content
        # ETag lets the polling dashboard skip re-downloading it (S6).
        return conditional_json(request, timeline.model_dump(mode="json"), max_age=60)

    return router
