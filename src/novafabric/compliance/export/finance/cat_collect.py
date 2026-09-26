"""Read-only collector for the NF-280 CAT-style agent-event trail (ADR-0159 D6).

Reads one sealed Run Capsule directory and returns the typed :class:`~.cat_trail.TrailFacts` that
:func:`~.cat_trail.build_cat_trail` renders. Only the sources named in the
:mod:`.cat_trail` stage mapping are read, and every event corresponds to exactly one recorded fact:

* ``capsule.yaml`` — ``run_id``, ``status``, ``created_at`` (→ ``origination``), ``finished_at``
  (→ ``disposition``), and the ADR-0251 ``evidence_digests`` map;
* ``model-calls.jsonl`` (→ ``decision``), ``tool-permission-events.jsonl`` and
  ``human_approvals.jsonl`` (→ ``authorization``), ``tool-calls.jsonl`` (→ ``action``) — one event
  per record, reduced to ids, identity refs, recorded outcome, and timestamps. Prompt / response
  text, tool arguments / results, and approval rationales are **never** read into the trail.

Hardening (shared with :mod:`.adverse_action_collect` via :mod:`._sealed_read`): every file is
opened read-only with ``O_NOFOLLOW`` (a symlinked capsule file is corrupt evidence, never
followed); a stream whose digest ``evidence_digests`` records but which is absent or not a regular
file (deleted, FIFO, directory) is sealed evidence that vanished — corrupt, never ``missing``;
reads are bounded
(manifest 8 MiB, each stream 128 MiB, ≤ :data:`MAX_EVENTS_PER_STREAM` rendered events per stream —
overflow is counted and reported ``partial``, never silent); every rendered recorded string is
capped at :data:`MAX_FIELD_TEXT` characters and scanned against the ADR-0009 secret rules — a hit
is suppressed (never rendered, named in ``suppressed_fields``). A recorded timestamp must be an
RFC 3339 date-time carrying a UTC offset. A digest that does not match the bytes on disk, a
malformed manifest / JSONL line, an over-long field, or an unparseable / offset-less timestamp
raises :class:`CorruptCapsuleError` (CLI exit 2). An *absent and unrecorded* source is a gap the
renderer reports as ``missing`` — never an error. Nothing is written; no network is touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._sealed_read import CorruptCapsuleError, read_sealed, sha256_bytes
from .cat_trail import EventFacts, Stage, StreamFacts, TrailFacts

__all__ = [
    "MANIFEST_MAX_BYTES",
    "MAX_EVENTS_PER_STREAM",
    "MAX_FIELD_TEXT",
    "STREAM_MAX_BYTES",
    "CorruptCapsuleError",
    "collect_trail_facts",
]

MANIFEST_MAX_BYTES = 8 * 1024 * 1024
STREAM_MAX_BYTES = 128 * 1024 * 1024
MAX_EVENTS_PER_STREAM = 1000
MAX_FIELD_TEXT = 1024
_MAX_TIMESTAMP_TEXT = 64


@dataclass(frozen=True)
class _StreamSpec:
    """How one JSONL stream maps onto lifecycle events (the explicit stage mapping)."""

    name: str
    stage: Stage
    event_type: str
    actor_kind: str
    id_key: str
    ts_key: str
    end_key: str | None
    actor_keys: tuple[str, ...]  # first recorded non-null value wins
    detail_key: str | None
    approver_key: str | None
    outcome_key: str | None
    caused_by_key: str | None
    subject_key: str | None


_STREAMS: tuple[_StreamSpec, ...] = (
    _StreamSpec(
        name="model-calls.jsonl",
        stage="decision",
        event_type="model_call",
        actor_kind="model",
        id_key="model_call_id",
        ts_key="started_at",
        end_key="finished_at",
        actor_keys=("gen_ai.response.model", "gen_ai.request.model"),
        detail_key="gen_ai.system",
        approver_key=None,
        outcome_key="status",
        caused_by_key=None,
        subject_key=None,
    ),
    _StreamSpec(
        name="tool-permission-events.jsonl",
        stage="authorization",
        event_type="tool_permission_decision",
        actor_kind="policy-principal",
        id_key="event_id",
        ts_key="capsule_timestamp_utc",
        end_key=None,
        actor_keys=("authorising_identity",),
        detail_key="policy_id",
        approver_key="approval_principal",
        outcome_key="decision",
        caused_by_key=None,
        subject_key="tool_name",
    ),
    _StreamSpec(
        name="human_approvals.jsonl",
        stage="authorization",
        event_type="human_approval",
        actor_kind="human-approver",
        id_key="event_id",
        ts_key="timestamp_utc",
        end_key=None,
        actor_keys=("approver_id",),
        detail_key="policy_version",
        approver_key=None,
        outcome_key="action",
        caused_by_key=None,
        subject_key="target_run_id",
    ),
    _StreamSpec(
        name="tool-calls.jsonl",
        stage="action",
        event_type="tool_call",
        actor_kind="tool",
        id_key="tool_call_id",
        ts_key="started_at",
        end_key="finished_at",
        actor_keys=("tool_name",),
        detail_key="tool_provider",
        approver_key=None,
        outcome_key="status",
        caused_by_key="agent_call_id",
        subject_key=None,
    ),
)


def _load_manifest(capsule_dir: Path) -> dict[str, Any]:
    import yaml

    raw = read_sealed(capsule_dir, "capsule.yaml", MANIFEST_MAX_BYTES, "manifest", {})
    if raw is None:
        raise CorruptCapsuleError(f"no capsule.yaml in {capsule_dir}")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CorruptCapsuleError(f"unparseable capsule.yaml in {capsule_dir}: {exc}") from exc
    if not isinstance(data, dict):
        raise CorruptCapsuleError(f"capsule.yaml in {capsule_dir} is not a mapping")
    return data


def _evidence_digests(manifest: dict[str, Any]) -> dict[str, str]:
    raw = manifest.get("evidence_digests")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CorruptCapsuleError("capsule.yaml evidence_digests is not a mapping")
    out: dict[str, str] = {}
    for rel, entry in raw.items():
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(digest, str):
            raise CorruptCapsuleError(f"evidence_digests[{rel!r}] carries no sha256")
        out[str(rel)] = digest
    return out


class _Scan:
    """Accumulates suppressed field names (per event) and rule ids (per source)."""

    def __init__(self) -> None:
        self.rules: list[str] = []

    def text(self, raw: Any, where: str, field: str, suppressed: list[str]) -> str | None:
        """A recorded scalar as text: length-capped, secret-scanned (``None`` when suppressed)."""
        if raw is None:
            return None
        if isinstance(raw, dict | list):
            raise CorruptCapsuleError(f"{where}.{field} must be a scalar, got {type(raw).__name__}")
        value = str(raw)
        if len(value) > MAX_FIELD_TEXT:
            raise CorruptCapsuleError(f"{where}.{field} exceeds {MAX_FIELD_TEXT} characters")
        from novafabric.capture.secrets import scan_text_rule_ids

        hits = scan_text_rule_ids(value)
        if not hits:
            return value
        suppressed.append(field)
        self.rules.extend(h for h in hits if h not in self.rules)
        return None


def _timestamp(raw: Any, where: str) -> tuple[str | None, str | None]:
    """Return ``(verbatim, utc_sortable)`` for a recorded timestamp (``(None, None)`` if absent).

    A YAML-parsed ``datetime`` is accepted; any value must carry a UTC offset — an offset-less
    time cannot be placed on a lifecycle timeline without guessing its zone.
    """
    if raw is None:
        return None, None
    if isinstance(raw, datetime):
        parsed, verbatim = raw, raw.isoformat()
    elif isinstance(raw, str):
        if len(raw) > _MAX_TIMESTAMP_TEXT:
            raise CorruptCapsuleError(f"{where} exceeds {_MAX_TIMESTAMP_TEXT} characters")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise CorruptCapsuleError(f"{where} is not an RFC 3339 date-time") from exc
        verbatim = raw
    else:
        raise CorruptCapsuleError(f"{where} must be a date-time string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CorruptCapsuleError(f"{where} carries no UTC offset")
    return verbatim, parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _first(record: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, Any]:
    for key in keys:
        if record.get(key) is not None:
            return key, record[key]
    return keys[0], None


def _event(
    spec: _StreamSpec,
    record: dict[str, Any],
    stream_index: int,
    lineno: int,
    line: bytes,
    scan: _Scan,
) -> EventFacts:
    where = f"{spec.name} line {lineno}"
    sup: list[str] = []

    def opt(key: str | None) -> str | None:
        return None if key is None else scan.text(record.get(key), where, key, sup)

    actor_key, actor_raw = _first(record, spec.actor_keys)
    ts, ts_utc = _timestamp(record.get(spec.ts_key), f"{where}.{spec.ts_key}")
    ended = None
    if spec.end_key is not None:
        ended, _ = _timestamp(record.get(spec.end_key), f"{where}.{spec.end_key}")
    return EventFacts(
        stage=spec.stage,
        event_type=spec.event_type,
        source_ref=f"{spec.name}#L{lineno}",
        stream_index=stream_index,
        line=lineno,
        record_sha256=sha256_bytes(line),
        event_id=opt(spec.id_key),
        timestamp=ts,
        timestamp_utc=ts_utc,
        ended_at=ended,
        actor_kind=spec.actor_kind,
        actor_ref=scan.text(actor_raw, where, actor_key, sup),
        actor_detail=opt(spec.detail_key),
        approver_ref=opt(spec.approver_key),
        outcome=opt(spec.outcome_key),
        caused_by_ref=opt(spec.caused_by_key),
        subject_ref=opt(spec.subject_key),
        suppressed_fields=sup,
    )


def _read_stream(
    capsule_dir: Path, spec: _StreamSpec, stream_index: int, digests: dict[str, str]
) -> tuple[list[EventFacts], StreamFacts]:
    # read_sealed raises when a stream sealed in ``digests`` is gone / not a regular file, and
    # verifies the recorded digest; ``None`` means absent AND unrecorded — a legitimate gap.
    raw = read_sealed(capsule_dir, spec.name, STREAM_MAX_BYTES, "event stream", digests)
    if raw is None:
        return [], StreamFacts(
            name=spec.name, stage=spec.stage, present=False, bound=False, total=0, rendered=0
        )
    recorded = digests.get(spec.name)
    scan = _Scan()
    events: list[EventFacts] = []
    total = 0
    for lineno, line in enumerate(raw.split(b"\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CorruptCapsuleError(f"{spec.name} line {lineno} is not JSON") from exc
        if not isinstance(record, dict):
            raise CorruptCapsuleError(f"{spec.name} line {lineno} is not a JSON object")
        total += 1
        if len(events) >= MAX_EVENTS_PER_STREAM:
            continue
        events.append(_event(spec, record, stream_index, lineno, line, scan))
    return events, StreamFacts(
        name=spec.name,
        stage=spec.stage,
        present=True,
        bound=recorded is not None,
        total=total,
        rendered=len(events),
        suppressed_rule_ids=scan.rules,
    )


def _manifest_events(manifest: dict[str, Any], status: str | None) -> list[EventFacts]:
    events: list[EventFacts] = []
    ts, ts_utc = _timestamp(manifest.get("created_at"), "capsule.yaml created_at")
    if ts is not None:
        events.append(
            EventFacts(
                stage="origination",
                event_type="run_started",
                source_ref="capsule.yaml#created_at",
                stream_index=0,
                line=0,
                timestamp=ts,
                timestamp_utc=ts_utc,
            )
        )
    ts, ts_utc = _timestamp(manifest.get("finished_at"), "capsule.yaml finished_at")
    if ts is not None:
        events.append(
            EventFacts(
                stage="disposition",
                event_type="run_finished",
                source_ref="capsule.yaml#finished_at",
                stream_index=0,
                line=0,
                timestamp=ts,
                timestamp_utc=ts_utc,
                outcome=status,
            )
        )
    return events


def collect_trail_facts(capsule_dir: Path) -> TrailFacts:
    """Read the NF-280 lifecycle facts from ``capsule_dir`` (strictly read-only, offline).

    Raises:
        CorruptCapsuleError: the manifest is absent/unreadable/malformed, a capsule file is a
            symlink, a stream sealed in ``evidence_digests`` is absent or not a regular file,
            a file exceeds its read bound, a sealed digest does not match the bytes on disk,
            a stream line is not a JSON object, a rendered field is over-long or non-scalar, or
            a recorded timestamp is unparseable or carries no UTC offset.
    """
    manifest = _load_manifest(capsule_dir)
    digests = _evidence_digests(manifest)
    scan = _Scan()
    sup: list[str] = []
    run_id = scan.text(
        manifest.get("run_id") or manifest.get("capsule_id"), "capsule.yaml", "run_id", sup
    )
    status = scan.text(manifest.get("status"), "capsule.yaml", "status", sup)
    events = _manifest_events(manifest, status)
    streams: list[StreamFacts] = []
    for index, spec in enumerate(_STREAMS, start=1):
        got, facts = _read_stream(capsule_dir, spec, index, digests)
        events.extend(got)
        streams.append(facts)
    seal = capsule_dir / ".seal" / "manifest.dsse"
    return TrailFacts(
        run_id=run_id if run_id is not None else capsule_dir.name,
        capsule_ref=str(capsule_dir),
        run_status=status,
        events=events,
        streams=streams,
        manifest_suppressed_rule_ids=scan.rules,
        seal_ref=".seal/manifest.dsse" if seal.is_file() and not seal.is_symlink() else None,
    )
