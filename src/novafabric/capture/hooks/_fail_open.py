"""The one guard every capture hook records through (fail-open, ADR-0021).

A hook sits *inside* the user's call: it runs the SDK/transport call, then
builds and writes a record. Building or writing can fail -- an SDK object that
does not serialise, a full disk, a bug in a field extractor -- and none of that
may reach the workload. The call's own return value, or its own exception,
must pass through unchanged.

Usage, at every record site::

    with fail_open("openai", MODEL_CALLS_STREAM):
        self._record(...)

A failure inside the block is swallowed and made visible two ways:

* a structured ``WARNING`` on ``novafabric.capture.hooks._fail_open`` with
  ``extra={"nova_event": "capture.record_dropped", "hook", "stream",
  "error_type"}`` -- the same shape as the MCP hook's own drop warning --
  once per (hook, stream, error type), later repeats at ``DEBUG`` so a
  persistent fault cannot flood the workload's log. It names the exception
  **type only**: an exception message may quote the payload being recorded
  (a prompt, a tool argument), which must never leak into a log;
* a drop counted, per stream, on the run's
  :class:`~novafabric.capture.event_recorder.EventRecorder` (when one is
  active), which lands in ``capture-health.json``.

Only ``Exception`` is caught: ``KeyboardInterrupt``/``SystemExit``/
cancellation are the workload's, never the recorder's to swallow.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from novafabric.capture.event_recorder import get_current_recorder

_log = logging.getLogger(__name__)

#: Capsule streams the hooks write (the keys of ``capture-health.json``).
MODEL_CALLS_STREAM = "model-calls.jsonl"
TOOL_CALLS_STREAM = "tool-calls.jsonl"

#: Bound on the distinct (hook, stream, error type) keys warned about.
_MAX_WARNED_KEYS = 256

_warned: set[tuple[str, str, str]] = set()
_warned_lock = threading.Lock()


def _first_time(key: tuple[str, str, str]) -> bool:
    with _warned_lock:
        if key in _warned:
            return False
        if len(_warned) >= _MAX_WARNED_KEYS:
            return False
        _warned.add(key)
        return True


def note_capture_failure(hook: str, stream: str, exc: BaseException) -> None:
    """Log and count one record *hook* could not write to *stream*. Never raises."""
    try:
        error_type = type(exc).__name__
        extra = {
            "nova_event": "capture.record_dropped",
            "hook": hook,
            "stream": stream,
            "error_type": error_type,
        }
        if _first_time((hook, stream, error_type)):
            _log.warning(
                "capture fail-open: the %s hook could not write a %s record (%s); "
                "the workload continues and the loss is counted in capture-health.json",
                hook, stream, error_type,
                extra=extra,
            )
        else:
            _log.debug("capture fail-open: the %s hook dropped a %s record (%s)",
                       hook, stream, error_type, extra=extra)
        recorder = get_current_recorder()
        if recorder is not None:
            # The recorder's own per-stream drop accounting, which
            # finalize_health() writes to capture-health.json.
            recorder.note_drop(stream)
    except Exception:  # noqa: BLE001 -- the guard itself must never raise
        pass


@contextmanager
def fail_open(hook: str, stream: str) -> Iterator[None]:
    """Run a record step; on failure log + count it instead of raising."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 -- capture must never block the workload
        note_capture_failure(hook, stream, exc)
