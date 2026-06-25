"""Capsule completeness assertion (ADR-0087(a), gap-008).

A :class:`CompletenessAssertion` declares **what the capsule claims to
contain** so an auditor can mechanically distinguish "no events occurred"
from "events were not captured" (EU AI Act Art. 12 distinction).

Honest limitation (recorded in ADR-0087): the assertion is a claim by the
capture plane about itself — drop counters and the active-hooks list let
auditors cross-check, but no stronger guarantee is claimed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from novafabric.evidence.intoto import dsse_sign, make_intoto_statement

COMPLETENESS_PREDICATE_TYPE = "https://novafabric.io/completeness/v0"

#: capsule event streams counted by the assertion
_EVENT_STREAMS = (
    "trace.jsonl",
    "model-calls.jsonl",
    "tool-calls.jsonl",
    "assets.jsonl",
    "lineage.jsonl",
    "network_events.jsonl",
    "file_events.jsonl",
    "tool-permission-events.jsonl",
)

#: record keys probed (in order) for an event timestamp
_TS_KEYS = ("started", "timestamp", "ts", "created_at", "finished")


class TimeWindow(BaseModel):
    """First/last event timestamps across all streams."""

    first: str
    last: str


class CompletenessAssertion(BaseModel):
    """What the capsule claims to contain (ADR-0087(a))."""

    schema_version: str = "0.1.0"
    run_id: str
    event_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Per-stream event counts (one entry per capsule stream)",
    )
    dropped_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Drop counters propagated from the capture plane",
    )
    capture_level: str = Field(
        default="unknown",
        description="Effective capture level at capture time, or 'unknown'",
    )
    time_window: TimeWindow | None = None
    active_hooks: list[str] = Field(
        default_factory=list,
        description="Capture hooks/integrations enabled for the run",
    )
    generated_at: str


def _count_lines(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text().splitlines() if line.strip())
    except OSError:
        return 0


def _probe_timestamps(path: Path) -> list[str]:
    stamps: list[str] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return stamps
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        for key in _TS_KEYS:
            value = record.get(key)
            if isinstance(value, str) and value:
                stamps.append(value)
                break
    return stamps


def _read_manifest(capsule_dir: Path) -> dict[str, Any]:
    path = capsule_dir / "capsule.yaml"
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text())
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def compute_completeness(capsule_dir: Path, run_id: str) -> CompletenessAssertion:
    """Compute the assertion from the capsule's actual contents."""
    counts: dict[str, int] = {}
    stamps: list[str] = []
    for stream in _EVENT_STREAMS:
        path = capsule_dir / stream
        if path.exists():
            counts[stream] = _count_lines(path)
            stamps.extend(_probe_timestamps(path))

    manifest = _read_manifest(capsule_dir)
    capture = manifest.get("capture", {}) if isinstance(manifest, dict) else {}
    capture_level = str(
        capture.get("level") or manifest.get("capture_level") or "unknown"
    )
    hooks_raw = capture.get("hooks") or manifest.get("hooks") or []
    active_hooks = [str(h) for h in hooks_raw] if isinstance(hooks_raw, list) else []
    dropped_raw = capture.get("dropped_counts") or {}
    dropped = (
        {str(k): int(v) for k, v in dropped_raw.items()}
        if isinstance(dropped_raw, dict)
        else {}
    )

    window = None
    if stamps:
        ordered = sorted(stamps)
        window = TimeWindow(first=ordered[0], last=ordered[-1])

    return CompletenessAssertion(
        run_id=run_id,
        event_counts=counts,
        dropped_counts=dropped,
        capture_level=capture_level,
        time_window=window,
        active_hooks=active_hooks,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def attest_completeness(
    capsule_dir: Path, assertion: CompletenessAssertion, signer: Any
) -> dict[str, Any]:
    """Wrap the assertion in an in-toto statement and DSSE-sign it.

    Subject is the capsule manifest (``capsule.yaml``) digest, matching the
    existing run-statement subject convention.
    """
    manifest_path = capsule_dir / "capsule.yaml"
    digest = hashlib.sha256(
        manifest_path.read_bytes() if manifest_path.exists() else b""
    ).hexdigest()
    statement = make_intoto_statement(
        predicate_type=COMPLETENESS_PREDICATE_TYPE,
        subject_name=f"run-capsule/{assertion.run_id}",
        subject_sha256=digest,
        predicate=assertion.model_dump(),
    )
    envelope: dict[str, Any] = dsse_sign(statement, signer)
    return envelope
