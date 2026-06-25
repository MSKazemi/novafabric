"""Bounded RAPL sampling daemon (ADR-0093 §A1).

A single background :class:`threading.Thread` snapshots cumulative energy
counters from an *injected* :class:`~novafabric.energy._readers.EnergyReader`
every ``interval_ms`` and appends compact raw lines to ``energy-samples.jsonl``
in the capsule directory. The attribution cold path (``attest_capsule``)
integrates those raw counters over the run window and apportions to actions.

Honoring ADR-0020/0021 (capture never blocks the workload, no heavy crypto/IO
on the hot path): the sampler does only a counter read + an in-memory append,
flushing a bounded buffer to disk on a threshold or on stop. It is **fail-open**
— a reader that raises (a counter that vanished mid-run, a permissions change)
logs a structured warning, records a *gap marker* sample, and never propagates
the exception into the workload.

The reader is injected at construction so tests drive the sampler with a
scripted fake reader: ``sample_once()`` is directly callable, so correctness
tests never depend on wall-clock timing.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from novafabric.energy._readers import EnergyReader

logger = logging.getLogger(__name__)

SAMPLES_FILE = "energy-samples.jsonl"

#: default in-memory buffer size before a flush to disk
_DEFAULT_BUFFER_MAX = 64


class EnergySampler:
    """Background sampler appending raw counter snapshots to the capsule.

    Parameters
    ----------
    capsule_dir:
        Directory the ``energy-samples.jsonl`` stream is written into.
    reader:
        An :class:`EnergyReader` (injected — pass a fake in tests). Its
        ``read_domains_uj`` returns cumulative microjoules per domain.
    interval_ms:
        Sampling period for the background loop, in milliseconds.
    buffer_max:
        Number of in-memory samples buffered before a flush to disk.
    """

    def __init__(
        self,
        capsule_dir: Path,
        reader: EnergyReader,
        *,
        interval_ms: int = 1000,
        buffer_max: int = _DEFAULT_BUFFER_MAX,
    ) -> None:
        self.capsule_dir = Path(capsule_dir)
        self.reader = reader
        self.interval_ms = max(0, int(interval_ms))
        self.buffer_max = max(1, int(buffer_max))
        self._path = self.capsule_dir / SAMPLES_FILE
        self._buffer: list[dict[str, object]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- single sample (directly testable, no sleeping) --------------------

    def sample_once(self) -> None:
        """Take one counter snapshot and buffer it. Fail-open, never raises.

        A reader exception or an unavailable reader records a *gap marker*
        (``gap: true``, ``domains_uj: {}``) so the cold-path integrator can see
        the discontinuity rather than silently interpolating across it.
        """
        gap = False
        domains: dict[str, int] = {}
        try:
            if self.reader.available():
                domains = dict(self.reader.read_domains_uj())
            else:
                gap = True
        except Exception:  # noqa: BLE001 — fail-open: a reader must never block
            logger.warning(
                "energy sampler reader %r raised; recording gap marker",
                getattr(self.reader, "name", self.reader),
                exc_info=True,
            )
            gap = True
            domains = {}

        record: dict[str, object] = {
            "ts": time.time(),
            "domains_uj": domains,
            "gap": gap,
        }
        with self._lock:
            self._buffer.append(record)
            if len(self._buffer) >= self.buffer_max:
                self._flush_locked()

    # --- flush -------------------------------------------------------------

    def flush(self) -> None:
        """Flush the in-memory buffer to ``energy-samples.jsonl`` (append)."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        try:
            self.capsule_dir.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as fh:
                for record in self._buffer:
                    fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError:
            logger.warning(
                "energy sampler could not flush %d samples to %s",
                len(self._buffer),
                self._path,
                exc_info=True,
            )
            return
        self._buffer.clear()

    # --- background thread -------------------------------------------------

    def _loop(self) -> None:
        interval_s = self.interval_ms / 1000.0
        # sample immediately so a fast start/stop still yields >=1 sample
        self.sample_once()
        while not self._stop.wait(interval_s):
            self.sample_once()

    def start(self) -> None:
        """Start the background sampling thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="novafabric-energy-sampler", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout_s: float = 5.0) -> None:
        """Signal the thread to stop, join it (bounded), and flush (idempotent)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_s)
        self._thread = None
        self.flush()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


__all__ = ["SAMPLES_FILE", "EnergySampler"]
