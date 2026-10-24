"""Ingest one capsule directory into the knowledge graph — the single definition.

``nova kg ingest`` (one dir and ``--all``), ``nova serve``'s ``/api/kg/ingest`` and its
auto-ingest loop each used to carry their own copy of this loop, and they had drifted:
one read only ``model-calls.jsonl``, one read through links, and all four counted an
event the pipeline dropped as "ingested" (review eval-adapters-query-10). They now all
call :func:`ingest_capsule_dir`.

Two guarantees beyond the loop itself:

* **Honest counts.** ``ingested`` is the number of events that added an edge
  observation; a malformed line, a non-object line or an event the pipeline cannot
  map is ``skipped``. An event file that is present but unreadable (a link, a FIFO, a
  directory) is listed in ``unreadable`` and never read.
* **Idempotent per capsule** (review eval-adapters-query-11). The capsule's events are
  flushed as one contribution keyed by :func:`capsule_identity`, which replaces that
  capsule's earlier contribution to each edge, so ingesting a capsule twice — or once
  by hand after ``nova serve`` auto-ingested it — leaves every ``call_count`` unchanged.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from novafabric._capsule_fs import (
    is_capsule_regular_file,
    read_capsule_member_or_none,
    read_capsule_text_or_none,
)
from novafabric.capture.record_roles import non_counting_model_call_ids

logger = logging.getLogger(__name__)

#: Read when present; ``events.jsonl`` (the internal KG schema) only when neither is.
PREFERRED_EVENT_FILES = ("model-calls.jsonl", "tool-calls.jsonl")
FALLBACK_EVENT_FILE = "events.jsonl"


@dataclass(frozen=True)
class CapsuleIngestResult:
    """What one :func:`ingest_capsule_dir` call did."""

    capsule_id: str
    files: tuple[str, ...]
    ingested: int
    skipped: int
    written: int
    unreadable: tuple[str, ...] = ()


def capsule_event_files(capsule_dir: Path) -> list[str]:
    """Event file names present in *capsule_dir*, in ingest order.

    Presence is by name (a link counts as present, so it is reported unreadable
    rather than silently missing).
    """
    present = [n for n in PREFERRED_EVENT_FILES if (capsule_dir / n).is_symlink()
               or (capsule_dir / n).exists()]
    if present:
        return present
    fallback = capsule_dir / FALLBACK_EVENT_FILE
    return [FALLBACK_EVENT_FILE] if fallback.is_symlink() or fallback.exists() else []


def capsule_identity(capsule_dir: Path) -> str:
    """The capsule's ``run_id`` from its manifest, else its resolved path.

    The ``run_id`` makes two copies of one capsule the same contributor; the path
    fallback still makes re-ingesting one event-only directory idempotent.
    """
    try:
        text = read_capsule_member_or_none(
            capsule_dir / "capsule.yaml", operation="knowledge-graph ingest"
        )
        manifest = yaml.safe_load(text) if text else None
    except (OSError, ValueError, yaml.YAMLError):
        manifest = None
    if isinstance(manifest, dict) and isinstance(manifest.get("run_id"), str):
        run_id: str = manifest["run_id"]
        if run_id:
            return run_id
    return str(capsule_dir.resolve())


def ingest_capsule_dir(
    pipeline: Any,
    capsule_dir: Path,
    *,
    novaseal_valid: bool = False,
    operation: str = "knowledge-graph ingest",
) -> CapsuleIngestResult:
    """Ingest *capsule_dir*'s event files through *pipeline* and flush them.

    *pipeline* is a :class:`~novafabric.kg.pipeline.KGIngestionPipeline`. Anything
    it had accumulated before is flushed first, additively, so it is never
    attributed to this capsule.
    """
    pipeline.flush_to_store()
    capsule_id = capsule_identity(capsule_dir)
    files = capsule_event_files(capsule_dir)
    ingested = 0
    skipped = 0
    unreadable: list[str] = []
    for name in files:
        if not is_capsule_regular_file(capsule_dir, name):
            unreadable.append(name)
            continue
        try:
            text = read_capsule_text_or_none(capsule_dir, name, operation=operation)
        except OSError as exc:
            logger.warning("KG ingest: cannot read %s/%s: %s", capsule_dir, name, exc)
            text = None
        if text is None:
            unreadable.append(name)
            continue
        # ADR-0305: transport records (incl. pre-ADR-0305 duplicates) are not calls.
        transport = (
            non_counting_model_call_ids(capsule_dir / name)
            if name == "model-calls.jsonl" else set()
        )
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(event, dict):
                skipped += 1
                continue
            if transport and event.get("model_call_id") in transport:
                continue  # not a call: neither ingested nor skipped
            if not event.get("capsule_id"):
                event = {**event, "capsule_id": capsule_id}
            if pipeline.ingest_event(event, novaseal_valid=novaseal_valid):
                ingested += 1
            else:
                skipped += 1
    written = pipeline.flush_to_store(contribution_key=capsule_id)
    return CapsuleIngestResult(
        capsule_id=capsule_id,
        files=tuple(files),
        ingested=ingested,
        skipped=skipped,
        written=written,
        unreadable=tuple(unreadable),
    )
