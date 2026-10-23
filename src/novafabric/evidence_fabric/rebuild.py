"""Offset-replay rebuild of the event buffer (gap-002 / SI-1, ADR-0020).

Productizes the SPK-COL-1-proven capability: a downstream store can be fully
rebuilt by replaying the durable NATS JetStream buffer from offset 0, with
per-``run_id`` partition ordering preserved and byte-equal digests.

Two layers:

* :func:`rebuild_from_events` — pure core: routes raw event payloads into
  per-run JSONL files, computes sha256 digests, checks per-run ``seq``
  monotonicity. No NATS dependency; fully unit-testable.
* :class:`OffsetReplayRebuilder` — async wrapper that drains a JetStream
  stream (ephemeral pull consumer, ``DeliverPolicy.ALL``) and feeds the core.
  Uses the same env conventions as
  :class:`~novafabric.evidence_fabric.nats_consumer.NATSJetStreamConsumer`
  (``NOVA_NATS_URL``, ``NOVA_NATS_STREAM``, ``NOVA_NATS_SUBJECT``).

Experimental (v0.51.0). Spike evidence: SPK-COL-1 PASS 3/3 on the lab server 2026-06-12
(byte-equal rebuild, per-run order, RF1 broker-restart no-loss).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import IO

from pydantic import BaseModel, Field

from novafabric._safe_path import is_safe_path_segment


class RebuildError(Exception):
    """Raised when the buffer cannot be drained or the target is invalid."""


class RunDigest(BaseModel):
    """Per-run rebuild facts."""

    run_id: str
    events: int
    sha256: str
    order_preserved: bool
    #: File under the target directory holding this run's events. Equal to
    #: ``<run_id>.jsonl`` unless the run_id is not a safe file name (see
    #: :func:`partition_file_name`). Optional, so older reports still load.
    partition_file: str | None = None


class RebuildReport(BaseModel):
    """Outcome of one offset-replay rebuild (experimental, ADR-0020)."""

    schema_version: str = "0.1.0"
    stream: str
    subject: str
    events_replayed: int
    runs: list[RunDigest] = Field(default_factory=list)
    order_preserved: bool
    target_dir: str

    @property
    def digest_map(self) -> dict[str, str]:
        return {r.run_id: r.sha256 for r in self.runs}


#: Partition for events that are not JSON objects or carry no ``run_id``.
UNATTRIBUTED = "_unattributed"


def partition_file_name(run_id: str) -> str:
    """The file name a run's events are written to, always one safe name in the target.

    ``run_id`` comes from the event, i.e. from whoever published to the broker.
    It is used as ``<run_id>.jsonl`` only when that is one safe path segment
    that does not start with ``.``; otherwise (absolute, ``..``, a separator,
    NUL, ...) the events go to ``_unsafe-<sha256(run_id)[:32]>.jsonl`` and the
    record keeps the original ``run_id`` inside it. An absolute or ``../``
    run_id once appended attacker JSON to any ``*.jsonl`` the user could write,
    such as the audit log (review trust-evidence-07).
    """
    name = f"{run_id}.jsonl"
    if is_safe_path_segment(name) and not name.startswith("."):
        return name
    digest = hashlib.sha256(run_id.encode("utf-8", "surrogateescape")).hexdigest()
    return f"_unsafe-{digest[:32]}.jsonl"


def _open_partition(target_dir: Path, name: str) -> IO[bytes]:
    """Create *name* in *target_dir* exclusively, never following a link."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target_dir / name, flags, 0o644)
    except FileExistsError as exc:
        raise RebuildError(
            f"{target_dir / name} already exists: rebuild into an empty directory, "
            "so each file holds exactly the events its digest describes"
        ) from exc
    return os.fdopen(fd, "wb")


def _existing_partitions(target_dir: Path) -> list[str]:
    if not target_dir.is_dir():
        return []
    return sorted(p.name for p in target_dir.iterdir() if p.name.endswith(".jsonl"))


def rebuild_from_events(
    events: Iterable[bytes],
    target_dir: Path,
    *,
    stream: str = "nova-evidence",
    subject: str = "nova.evidence.>",
) -> RebuildReport:
    """Route raw event payloads into per-run JSONL files under *target_dir*.

    Pure core of the rebuild: deterministic, no broker access. Events that
    are not JSON objects or carry no ``run_id`` are routed to the
    ``_unattributed`` partition rather than dropped (an auditor must see
    them). ``seq`` fields, when present, must be strictly increasing per run.

    Each run's file name comes from :func:`partition_file_name`, and every file
    is created exclusively: a target that already holds one raises
    :class:`RebuildError` rather than appending a second pass to it.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    hashers: dict[str, hashlib._Hash] = {}  # noqa: SLF001 - stdlib type alias
    ordered: dict[str, bool] = {}
    last_seq: dict[str, int] = {}
    handles: dict[str, IO[bytes]] = {}
    files: dict[str, str] = {}

    try:
        for payload in events:
            run_id = UNATTRIBUTED
            seq: int | None = None
            try:
                record = json.loads(payload)
                if isinstance(record, dict):
                    run_id = str(record.get("run_id") or UNATTRIBUTED)
                    raw_seq = record.get("seq")
                    seq = int(raw_seq) if isinstance(raw_seq, int) else None
            except json.JSONDecodeError:
                pass
            fh = handles.get(run_id)
            if fh is None:
                name = partition_file_name(run_id)
                if name in files.values():  # pragma: no cover - a sha256 prefix collision
                    raise RebuildError(f"two run_ids map to partition {name}")
                fh = _open_partition(target_dir, name)
                handles[run_id] = fh
                files[run_id] = name
                counts[run_id] = 0
                hashers[run_id] = hashlib.sha256()
                ordered[run_id] = True
            line = payload if payload.endswith(b"\n") else payload + b"\n"
            fh.write(line)
            hashers[run_id].update(line)
            counts[run_id] += 1
            if seq is not None:
                if run_id in last_seq and seq <= last_seq[run_id]:
                    ordered[run_id] = False
                last_seq[run_id] = seq
    finally:
        for fh in handles.values():
            fh.close()

    runs = [
        RunDigest(
            run_id=run_id,
            events=counts[run_id],
            sha256=hashers[run_id].hexdigest(),
            order_preserved=ordered[run_id],
            partition_file=files[run_id],
        )
        for run_id in sorted(counts)
    ]
    return RebuildReport(
        stream=stream,
        subject=subject,
        events_replayed=sum(counts.values()),
        runs=runs,
        order_preserved=all(ordered.values()) if ordered else True,
        target_dir=str(target_dir),
    )


class OffsetReplayRebuilder:
    """Drain a JetStream stream from offset 0 into per-run JSONL files."""

    def __init__(
        self,
        nats_url: str | None = None,
        stream: str | None = None,
        subject: str | None = None,
        batch_size: int = 500,
        idle_timeout_s: float = 5.0,
    ) -> None:
        self._nats_url = nats_url or os.environ.get(
            "NOVA_NATS_URL", "nats://localhost:4222"
        )
        self._stream = stream or os.environ.get("NOVA_NATS_STREAM", "nova-evidence")
        self._subject = subject or os.environ.get(
            "NOVA_NATS_SUBJECT", "nova.evidence.>"
        )
        self._batch_size = batch_size
        self._idle_timeout_s = idle_timeout_s

    async def drain(self) -> list[bytes]:
        """Replay every message in the stream from offset 0 (ephemeral pull)."""
        try:
            import nats
            from nats.js.api import ConsumerConfig, DeliverPolicy
        except ImportError as exc:  # pragma: no cover - import guard
            raise RebuildError(
                "nats-py is required: pip install 'novafabric[scale]'"
            ) from exc

        payloads: list[bytes] = []
        nc = await nats.connect(
            self._nats_url,
            connect_timeout=3,
            allow_reconnect=False,
            max_reconnect_attempts=1,
        )
        try:
            js = nc.jetstream()
            sub = await js.pull_subscribe(
                self._subject,
                stream=self._stream,
                config=ConsumerConfig(deliver_policy=DeliverPolicy.ALL),
            )
            try:
                while True:
                    try:
                        batch = await sub.fetch(
                            self._batch_size, timeout=self._idle_timeout_s
                        )
                    except Exception:
                        break  # idle — stream drained
                    if not batch:
                        break
                    for msg in batch:
                        payloads.append(bytes(msg.data))
                        await msg.ack()
            finally:
                await sub.unsubscribe()
        finally:
            await nc.close()
        return payloads

    async def rebuild(self, target_dir: Path) -> RebuildReport:
        """Drain the buffer and materialize it under *target_dir*.

        Refuses (:class:`RebuildError`) a target that already holds ``*.jsonl``
        files before touching the broker.
        """
        existing = _existing_partitions(target_dir)
        if existing:
            raise RebuildError(
                f"{target_dir} already holds {len(existing)} partition file(s) "
                f"({existing[0]}, ...): rebuild into an empty directory"
            )
        events = await self.drain()
        return rebuild_from_events(
            events, target_dir, stream=self._stream, subject=self._subject
        )
