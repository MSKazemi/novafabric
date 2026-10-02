"""Threads started inside a capture file into that capture (ADR-0224 D3 ▸ Amendment 4).

Phase 2 and Amendment 3 scoped a capture to its *task*. A ``threading.Thread``
starts with an empty context, so a thread a participant capture started resolved
the process fallback — the hook owner's writer — and filed the participant's
model calls into the **owner's** capsule. ``ThreadPoolExecutor`` work did the
same, and a pool is worse: its workers outlive the capture that started them, so
naive start-time inheritance would pin every later item to the first capture.

These tests fire real model calls through a real hook layer built with the
owner's writer (as the installed layer is) and assert on capsule contents.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from novafabric.capture import event_recorder as er
from novafabric.capture import hooks
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import get_current_writer

_TIMEOUT = 10.0


@pytest.fixture(autouse=True)
def _clean_state():
    hooks.uninstall_all()
    hooks._contended_owners.clear()
    yield
    leaked = get_current_writer(None)
    hooks.uninstall_all()
    hooks._contended_owners.clear()
    assert leaked is None, "a capture binding leaked out of the test"


def _writer(base: Path, run_id: str) -> CapsuleWriter:
    w = CapsuleWriter(run_id=run_id, base_dir=base)
    w.open()
    return w


def _model_calls(base: Path, run_id: str) -> int:
    path = base / run_id / "model-calls.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def _fire(layer: object) -> None:
    req = MagicMock()
    req.url = "https://api.openai.com/v1/chat/completions"
    req.body = json.dumps(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    ).encode()
    layer._wrapped_send(  # type: ignore[attr-defined]
        req, original=MagicMock(return_value=MagicMock(status_code=200))
    )


def _layer(writer: CapsuleWriter) -> object:
    from novafabric.capture.hooks._requests import RequestsHook

    return RequestsHook(writer=writer, parent_span_id="0" * 16)


def _join(t: threading.Thread) -> None:
    t.join(_TIMEOUT)
    assert not t.is_alive(), "thread did not finish"


def _in_thread(fn) -> None:  # type: ignore[no-untyped-def]
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=_target)
    t.start()
    _join(t)
    if errors:
        raise errors[0]


def test_a_thread_started_by_a_participant_files_into_the_participant(tmp_path: Path) -> None:
    wa, wb = _writer(tmp_path, "run-A"), _writer(tmp_path, "run-B")
    owner_layer = _layer(wa)
    tok_a = hooks.install_all(writer=wa, parent_span_id="a" * 16)

    def participant() -> None:
        tok_b = hooks.install_all(writer=wb, parent_span_id="b" * 16)
        assert tok_b.startswith("par:")
        child = threading.Thread(target=_fire, args=(owner_layer,))
        child.start()
        _join(child)
        hooks.uninstall_all(tok_b)

    _in_thread(participant)
    hooks.uninstall_all(tok_a)

    assert (_model_calls(tmp_path, "run-B"), _model_calls(tmp_path, "run-A")) == (1, 0), (
        "the participant's child thread filed into the owner's capsule"
    )


def test_pool_items_follow_the_submitter_not_the_worker(tmp_path: Path) -> None:
    """A worker started by B must not pin a later item from C to B."""
    wa, wb, wc = (_writer(tmp_path, f"run-{x}") for x in "ABC")
    owner_layer = _layer(wa)
    tok_a = hooks.install_all(writer=wa, parent_span_id="a" * 16)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    b_bound, c_done = threading.Event(), threading.Event()

    def capture_b() -> None:
        tok_b = hooks.install_all(writer=wb, parent_span_id="b" * 16)
        # B's submit starts the pool's only worker.
        pool.submit(_fire, owner_layer).result(_TIMEOUT)
        b_bound.set()
        assert c_done.wait(_TIMEOUT)
        hooks.uninstall_all(tok_b)

    def capture_c() -> None:
        assert b_bound.wait(_TIMEOUT)
        tok_c = hooks.install_all(writer=wc, parent_span_id="c" * 16)
        pool.submit(_fire, owner_layer).result(_TIMEOUT)
        hooks.uninstall_all(tok_c)
        c_done.set()

    tb = threading.Thread(target=capture_b)
    tc = threading.Thread(target=capture_c)
    tb.start()
    tc.start()
    _join(tb)
    _join(tc)
    # And an item submitted from the owner's own context is the owner's.
    pool.submit(_fire, owner_layer).result(_TIMEOUT)
    pool.shutdown(wait=True)
    hooks.uninstall_all(tok_a)

    counts = tuple(_model_calls(tmp_path, f"run-{x}") for x in "ABC")
    assert counts == (1, 1, 1), f"(A, B, C) model calls were {counts}"


def test_a_thread_that_outlives_its_capture_does_not_keep_filing_into_it(
    tmp_path: Path,
) -> None:
    wa, wb = _writer(tmp_path, "run-A"), _writer(tmp_path, "run-B")
    owner_layer = _layer(wa)
    tok_a = hooks.install_all(writer=wa, parent_span_id="a" * 16)
    released, fired = threading.Event(), threading.Event()
    holder: list[threading.Thread] = []

    def lingering() -> None:
        assert released.wait(_TIMEOUT)
        _fire(owner_layer)
        fired.set()

    def participant() -> None:
        tok_b = hooks.install_all(writer=wb, parent_span_id="b" * 16)
        t = threading.Thread(target=lingering)
        t.start()
        holder.append(t)
        hooks.uninstall_all(tok_b)

    _in_thread(participant)
    released.set()
    _join(holder[0])
    assert fired.is_set()
    hooks.uninstall_all(tok_a)

    assert _model_calls(tmp_path, "run-B") == 0, "a finished capture kept receiving events"
    assert _model_calls(tmp_path, "run-A") == 1


def test_teardown_restores_the_threading_entry_points(tmp_path: Path) -> None:
    start, submit = threading.Thread.start, concurrent.futures.ThreadPoolExecutor.submit
    tok = hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="a" * 16)
    # Not vacuous: the entry points really were patched while installed.
    assert threading.Thread.start is not start
    assert concurrent.futures.ThreadPoolExecutor.submit is not submit
    hooks.uninstall_all(tok)
    assert threading.Thread.start is start
    assert concurrent.futures.ThreadPoolExecutor.submit is submit


def test_a_propagation_failure_never_breaks_the_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tok = hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="a" * 16)

    def boom() -> None:
        raise RuntimeError("capture internals broke")

    monkeypatch.setattr(er, "current_scope_snapshot", boom)
    ran: list[int] = []
    t = threading.Thread(target=ran.append, args=(1,))
    t.start()
    _join(t)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: 2).result(_TIMEOUT) == 2
    hooks.uninstall_all(tok)
    assert ran == [1]


def test_workload_exceptions_in_pool_items_propagate_unchanged(tmp_path: Path) -> None:
    tok = hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="a" * 16)

    def fail() -> None:
        raise ValueError("the workload's own error")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(fail)
        with pytest.raises(ValueError, match="workload's own error"):
            fut.result(_TIMEOUT)
    hooks.uninstall_all(tok)


def test_a_foreign_patch_over_ours_is_left_alone_and_reinstall_does_not_recurse(
    tmp_path: Path,
) -> None:
    from novafabric.capture.hooks import _threads

    true_start = threading.Thread.start
    tok = hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="a" * 16)
    ours = threading.Thread.start

    def foreign(self, *a, **k):  # type: ignore[no-untyped-def]
        return ours(self, *a, **k)

    threading.Thread.start = foreign  # type: ignore[method-assign]
    try:
        hooks.uninstall_all(tok)
        assert threading.Thread.start is foreign, "a foreign patch was removed"
        assert not _threads._active
        tok = hooks.install_all(writer=_writer(tmp_path, "run-B"), parent_span_id="b" * 16)
        ran: list[int] = []
        t = threading.Thread(target=ran.append, args=(1,))
        t.start()
        _join(t)
        assert ran == [1]
        hooks.uninstall_all(tok)
    finally:
        threading.Thread.start = true_start  # type: ignore[method-assign]
        _threads._orig_start = None
