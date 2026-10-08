"""Keep an adapter capsule open for as long as a streamed run is still running.

A streaming entry point returns *before* the run finishes: the model call
happens while the caller iterates a generator, or inside an ``async with``
body. Closing the capsule when the method returns writes an empty, successful
capsule and releases the wire hooks before the first token arrives. These
helpers move the close to where the run really ends, and record *how* it ended:

* exhausted / exited normally -> ``success``
* raised mid-stream            -> ``failure`` (the exception still propagates)
* closed or dropped early      -> ``partial``, with ``metadata.partial_reason``
* cancelled                    -> ``partial`` (``cancelled``)

Every closer is idempotent: a generator's ``finally``, a ``weakref.finalize``
fallback, and a watcher thread may each try to close the same capsule, and the
first one wins.

While a guarded step runs, the adapter's re-entrancy flag is set, so a nested
call made while producing the stream records into this capsule instead of
opening a second one that would steal the wire hooks.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from novafabric.adapters._capsule import AdapterCapture

#: Reason recorded when the caller stopped reading before the stream ended.
ABANDONED = "abandoned"
#: Reason recorded when the run was cancelled before it ended.
CANCELLED = "cancelled"

#: Watcher tasks are only weakly referenced by the event loop; hold them here
#: until they finish, or the garbage collector may drop one mid-run.
_WATCHERS: set[asyncio.Task[None]] = set()


class Closer:
    """Close one capsule exactly once, whoever gets there first."""

    def __init__(self, cap: AdapterCapture) -> None:
        self._cap = cap
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(
        self, *, error: BaseException | None = None, partial: str | None = None
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if error is not None:
            self._cap.fail(error)
        elif partial is not None:
            self._cap.mark_partial(partial)
        self._cap.finish()


def _close_if_dropped(closer: Closer) -> None:
    """``weakref.finalize`` callback: the stream object died unread."""
    closer.close(partial=ABANDONED)


def close_when_collected(obj: Any, closer: Closer) -> None:
    """Close as ``partial`` if *obj* is garbage-collected before the stream ends.

    A generator the caller never started has no frame, so closing it runs none
    of its code — without this, a streaming response that is created and then
    ignored would hold the wire hooks until the process exits.
    """
    try:
        weakref.finalize(obj, _close_if_dropped, closer)
    except TypeError:  # pragma: no cover - not weak-referenceable
        pass


def guard_iterator(
    inner: Iterator[Any],
    closer: Closer,
    in_flight: contextvars.ContextVar[bool],
) -> Iterator[Any]:
    """Yield from *inner*; close the capsule when it ends, however it ends."""
    error: BaseException | None = None
    exhausted = False
    try:
        while True:
            token = in_flight.set(True)
            try:
                item = next(inner)
            except StopIteration:
                exhausted = True
                return
            finally:
                in_flight.reset(token)
            yield item
    except Exception as exc:
        error = exc
        raise
    finally:
        if not exhausted:
            close = getattr(inner, "close", None)
            if callable(close):
                close()
        closer.close(error=error, partial=None if exhausted else ABANDONED)


async def guard_async_iterator(
    inner: AsyncIterator[Any],
    closer: Closer,
    in_flight: contextvars.ContextVar[bool],
) -> AsyncIterator[Any]:
    """Async twin of :func:`guard_iterator`."""
    error: BaseException | None = None
    reason: str | None = ABANDONED
    try:
        while True:
            token = in_flight.set(True)
            try:
                item = await inner.__anext__()
            except StopAsyncIteration:
                reason = None
                return
            finally:
                in_flight.reset(token)
            yield item
    except asyncio.CancelledError:
        reason = CANCELLED
        raise
    except Exception as exc:
        error = exc
        raise
    finally:
        if reason is not None:
            aclose = getattr(inner, "aclose", None)
            if callable(aclose):
                await aclose()
        closer.close(error=error, partial=reason)


def watch_thread(thread: threading.Thread, closer: Closer, outcome: Callable[[], Any]) -> None:
    """Close when *thread* (a framework's background stream consumer) finishes.

    *outcome* is read after the join: an exception instance means the stream
    failed; anything else means it completed.
    """

    def _watch() -> None:
        thread.join()
        result = outcome()
        closer.close(error=result if isinstance(result, BaseException) else None)

    threading.Thread(
        target=_watch, name="novafabric-stream-close", daemon=False
    ).start()


def watch_awaitable(awaitable: Any, closer: Closer) -> None:
    """Close when *awaitable* settles, without ever cancelling it.

    ``asyncio.wait`` is used rather than ``await``: if the watcher itself is
    cancelled (event-loop shutdown), a direct ``await`` would propagate the
    cancellation into the run being watched.
    """
    fut = asyncio.ensure_future(awaitable)

    async def _watch() -> None:
        try:
            await asyncio.wait({fut})
        except asyncio.CancelledError:
            if not fut.done() or fut.cancelled():
                closer.close(partial=CANCELLED)
                raise
        # The run may have settled in the same loop turn the watcher was
        # cancelled in (event-loop shutdown); its own outcome is the truth.
        if fut.cancelled():
            closer.close(partial=CANCELLED)
            return
        closer.close(error=fut.exception())

    task = asyncio.get_running_loop().create_task(_watch())
    _WATCHERS.add(task)
    task.add_done_callback(_WATCHERS.discard)


class GuardedAsyncContext:
    """Wrap a framework's ``async with`` run so the capsule spans the body.

    The capsule opens on ``__aenter__`` — not when the method is called — so a
    context manager built in one place and entered in another (Pydantic AI's
    ``run_stream_sync`` enters ``run_stream`` in an owner task) is still
    captured once, in the task that runs it. If a capture is already in flight
    when it is entered, it records into that one.

    *settle* inspects the value the context yielded once the body exits
    cleanly, and returns a partial reason (``"abandoned"``) when the run had
    not finished, or ``None`` when it had.
    """

    def __init__(
        self,
        cm: Any,
        *,
        begin: Callable[[], AdapterCapture],
        in_flight: contextvars.ContextVar[bool],
        settle: Callable[[Any], str | None],
    ) -> None:
        self._cm = cm
        self._begin = begin
        self._in_flight = in_flight
        self._settle = settle
        self._cap: AdapterCapture | None = None
        self._token: contextvars.Token[bool] | None = None
        self._value: Any = None

    async def __aenter__(self) -> Any:
        if self._in_flight.get():
            return await self._cm.__aenter__()
        cap = self._begin()
        token = self._in_flight.set(True)
        try:
            self._value = await self._cm.__aenter__()
        except BaseException as exc:
            self._in_flight.reset(token)
            _record(cap, exc)
            cap.finish()
            raise
        self._cap, self._token = cap, token
        return self._value

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        cap, token = self._cap, self._token
        if cap is None or token is None:
            return await self._cm.__aexit__(exc_type, exc, tb)
        self._cap = self._token = None
        try:
            suppressed = await self._cm.__aexit__(exc_type, exc, tb)
        except BaseException as raised:
            _record(cap, raised)
            raise
        else:
            if exc is not None and not suppressed:
                _record(cap, exc)
            else:
                reason = self._settle(self._value)
                if reason is not None:
                    cap.mark_partial(reason)
            return suppressed
        finally:
            try:
                self._in_flight.reset(token)
            except ValueError:  # exited in a different context than entered
                pass
            cap.finish()


def _record(cap: AdapterCapture, exc: BaseException) -> None:
    """Record how a run that raised *exc* ended."""
    if isinstance(exc, asyncio.CancelledError):
        cap.mark_partial(CANCELLED)
    elif isinstance(exc, Exception):
        cap.fail(exc)
    else:  # GeneratorExit, KeyboardInterrupt: stopped, not failed
        cap.mark_partial(ABANDONED)
