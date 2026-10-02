"""Carry the capture scope across thread boundaries (ADR-0224 D3 ▸ Amendment 4).

A :class:`threading.Thread` starts with an **empty** context, so a thread a
capture starts resolves no capture scope and falls back to the process recorder
and the hook owner's writer. For the owner that is the right answer by accident;
for a concurrent *participant* capture it files the thread's model calls and
network events into the owner's capsule — wrong evidence, not missing evidence.

While the owner's patch layer is installed this hook makes two entry points
carry the scope that is live where the work was *handed off*:

``threading.Thread.start``
    The starting context's innermost live scope is bound for the thread's
    ``run``. Covers ``threading.Timer`` and ``Thread`` subclasses, since they
    all go through ``start``.
``concurrent.futures.ThreadPoolExecutor.submit``
    Bound **per work item**, not per worker. A pool's workers outlive the
    capture that started them, so start-time inheritance alone would pin every
    later item — from any capture — to the first one. Each item instead runs
    under its submitter's scope, ``None`` included, and the worker keeps
    nothing. ``Executor.map`` and ``loop.run_in_executor`` go through
    ``submit``; ``asyncio.to_thread`` already copies its context.

Invariants:

- **Fail-open.** Any failure in the propagation path runs the workload exactly
  as it would have run unpatched. Workload exceptions propagate unchanged.
- **Bounded, no cross-run state.** The only thing carried is a reference to the
  scope object. Scopes are revocable (Amendment 3): a thread that outlives its
  capture resolves the fallback, never the finished capture. Nothing is
  registered globally per thread or per item.
- **No import-time side effects.** Patching happens in :meth:`install` only.

Not covered (stated, not rounded to zero): threads started *before* the capture
bound its scope, threads started by native code, and pools other than
``ThreadPoolExecutor`` whose long-lived workers pick work off a queue. Those
still resolve the fallback, which is why ``installed-contended`` still warns.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Any

from novafabric.capture import event_recorder as _er

#: Process-wide patch state, guarded by ``_lock``. The originals are captured
#: once; ``_active`` gates the wrappers so that a teardown which cannot restore
#: the original (something else patched over ours) leaves an inert wrapper in
#: the chain rather than a re-patch that would recurse.
_lock = threading.Lock()
_orig_start: Any = None
_orig_submit: Any = None
_active = False
_owner: object | None = None
_executor_worker_fn: Any = None


def _executor_worker() -> Any:
    try:
        from concurrent.futures import thread as _cft

        return getattr(_cft, "_worker", None)
    except Exception:  # pragma: no cover — defensive
        return None


def _patched_start(self: threading.Thread, *args: Any, **kwargs: Any) -> Any:
    if _active:
        try:
            snapshot = _er.current_scope_snapshot()
            # Pool workers get per-item propagation via submit; letting them
            # also inherit at start would make the starting capture their
            # default for the life of the pool.
            if snapshot is not None and getattr(self, "_target", None) is not _executor_worker_fn:
                run = self.run

                def _run_in_scope() -> None:
                    try:
                        _er.run_with_scope(snapshot, run)
                    finally:
                        # A finished Thread object must not retain the scope.
                        self.__dict__.pop("run", None)

                self.run = _run_in_scope  # type: ignore[method-assign]
        except Exception:
            pass  # fail-open: start the thread exactly as unpatched
    return _orig_start(self, *args, **kwargs)


def _patched_submit(self: Any, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    if _active:
        try:
            snapshot = _er.current_scope_snapshot()
        except Exception:
            pass  # fail-open: submit exactly as unpatched
        else:
            # Wrapped even when *snapshot* is None: the item must run with no
            # scope rather than whatever the worker thread happens to hold.
            return _orig_submit(self, _er.run_with_scope, snapshot, fn, *args, **kwargs)
    return _orig_submit(self, fn, *args, **kwargs)


class ThreadContextHook:
    """Patch ``Thread.start`` and ``ThreadPoolExecutor.submit`` while installed.

    Same constructor shape as the SDK hooks so ``capture.hooks`` can hold it in
    ``_installed``; it records nothing itself.
    """

    def __init__(self, writer: Any = None, parent_span_id: str = "") -> None:
        self._installed = False

    def install(self) -> None:
        global _orig_start, _orig_submit, _active, _owner, _executor_worker_fn
        with _lock:
            if _owner is not None:
                return  # one patch layer per process, like the other hooks
            if _executor_worker_fn is None:
                _executor_worker_fn = _executor_worker()
            if _orig_start is None:
                _orig_start = threading.Thread.start
                threading.Thread.start = _patched_start  # type: ignore[method-assign]
            if _orig_submit is None:
                _orig_submit = concurrent.futures.ThreadPoolExecutor.submit
                concurrent.futures.ThreadPoolExecutor.submit = _patched_submit  # type: ignore[method-assign]
            _active = True
            _owner = self
            self._installed = True

    def uninstall(self) -> None:
        global _orig_start, _orig_submit, _active, _owner
        with _lock:
            if not self._installed or _owner is not self:
                return
            _active = False
            # Restore only what is still ours on top. If something patched over
            # us, our wrapper stays in its chain, inert, and is reused by the
            # next install instead of being wrapped a second time.
            if threading.Thread.start is _patched_start:
                threading.Thread.start = _orig_start  # type: ignore[method-assign]
                _orig_start = None
            if concurrent.futures.ThreadPoolExecutor.submit is _patched_submit:
                concurrent.futures.ThreadPoolExecutor.submit = _orig_submit  # type: ignore[method-assign]
                _orig_submit = None
            _owner = None
            self._installed = False
