# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Concurrent in-process captures stay independent end to end (ADR-0224 D3 ▸ Amendment 3).

Phase 2 gave the race loser a participant token and a task-scoped binding. Two
defects survived it, both reproduced against the real module before the fix:

1. **A release from another context did not release.** ``unbind_capture`` popped
   the registry entry but could only clear the ``ContextVar`` in the *caller's*
   context. When the hook owner finished first, its teardown "released" the
   participant from the owner's thread; the participant's own release then found
   nothing to do, and its writer stayed bound in its thread. The next capture to
   run there filed its model calls and network events into the finished
   capture's capsule.
2. **The marker over-claimed.** That participant still reported
   ``scoped-concurrent`` — "complete for everything it did" — although the patch
   layer was removed mid-run.

These tests pin the contract with real threads and real asyncio tasks, firing
events through a real hook so the assertion is on capsule contents, not on
which object a lookup returns.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from novafabric.capture import event_recorder as er
from novafabric.capture import hooks
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import (
    bind_capture,
    get_current_recorder,
    get_current_writer,
    unbind_capture,
)

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
    """Fire one model call through a patch layer, in the calling context."""
    req = MagicMock()
    req.url = "https://api.openai.com/v1/chat/completions"
    req.body = json.dumps(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    ).encode()
    layer._wrapped_send(  # type: ignore[attr-defined]
        req, original=MagicMock(return_value=MagicMock(status_code=200))
    )


def _layer(writer: CapsuleWriter) -> object:
    """A real hook constructed with *writer*, standing in for the owner's layer."""
    from novafabric.capture.hooks._requests import RequestsHook

    return RequestsHook(writer=writer, parent_span_id="0" * 16)


def _run_in_thread(fn) -> None:  # type: ignore[no-untyped-def]
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            fn()
        except BaseException as exc:  # surfaced to the test thread below
            errors.append(exc)

    t = threading.Thread(target=_target)
    t.start()
    t.join(_TIMEOUT)
    assert not t.is_alive(), "thread did not finish"
    if errors:
        raise errors[0]


# --------------------------------------------------------------------------- #
# AC1 — a release made in any context takes effect in the binding context
# --------------------------------------------------------------------------- #


class TestReleaseIsGlobal:
    def test_release_from_another_thread_reaches_the_binding_thread(
        self, tmp_path: Path
    ) -> None:
        w = _writer(tmp_path, "run-b")
        bound, released, seen = threading.Event(), threading.Event(), []
        box: list[str] = []

        def _binder() -> None:
            box.append(bind_capture(writer=w))
            bound.set()
            assert released.wait(_TIMEOUT)
            seen.append(get_current_writer(None))

        t = threading.Thread(target=_binder)
        t.start()
        assert bound.wait(_TIMEOUT)
        assert unbind_capture(box[0]) is True
        released.set()
        t.join(_TIMEOUT)
        assert seen == [None], "the binding survived a release made elsewhere"

    def test_release_from_another_task_reaches_the_binding_task(
        self, tmp_path: Path
    ) -> None:
        w = _writer(tmp_path, "run-b")

        async def _main() -> object:
            bound, released = asyncio.Event(), asyncio.Event()
            box: list[str] = []

            async def binder() -> object:
                box.append(bind_capture(writer=w))
                bound.set()
                await released.wait()
                return get_current_writer(None)

            task = asyncio.create_task(binder())
            await bound.wait()
            assert await asyncio.create_task(_release(box[0]))
            released.set()
            return await task

        async def _release(handle: str) -> bool:
            return unbind_capture(handle)

        assert asyncio.run(_main()) is None

    def test_releasing_a_nested_capture_restores_the_outer_one(
        self, tmp_path: Path
    ) -> None:
        outer, inner = _writer(tmp_path, "outer"), _writer(tmp_path, "inner")
        h_outer = bind_capture(writer=outer)
        h_inner = bind_capture(writer=inner)
        assert get_current_writer(None) is inner
        unbind_capture(h_inner)
        assert get_current_writer(None) is outer
        unbind_capture(h_outer)
        assert get_current_writer(None) is None

    def test_releasing_the_outer_capture_first_leaves_the_inner_bound(
        self, tmp_path: Path
    ) -> None:
        outer, inner = _writer(tmp_path, "outer"), _writer(tmp_path, "inner")
        h_outer = bind_capture(writer=outer)
        h_inner = bind_capture(writer=inner)
        unbind_capture(h_outer)
        assert get_current_writer(None) is inner
        unbind_capture(h_inner)
        assert get_current_writer(None) is None

    def test_a_long_dead_chain_is_walked_boundedly(self, tmp_path: Path) -> None:
        handles = [
            bind_capture(writer=_writer(tmp_path, f"r{i}"))
            for i in range(er._MAX_SCOPE_DEPTH + 5)
        ]
        # Release from a different thread so this context keeps every dead link.
        _run_in_thread(lambda: [unbind_capture(h) for h in handles])
        assert get_current_writer(None) is None
        assert get_current_recorder() is None
        er._scope_var.set(None)


# --------------------------------------------------------------------------- #
# AC2 — the owner finishing first leaves no stale binding behind (the defect)
# --------------------------------------------------------------------------- #


class TestOwnerFinishesFirst:
    def test_threads_next_capture_in_the_participants_thread_files_into_itself(
        self, tmp_path: Path
    ) -> None:
        wa, wb, wc = (_writer(tmp_path, n) for n in ("run-a", "run-b", "run-c"))
        owner_in, part_in, owner_out = (threading.Event() for _ in range(3))

        def _owner() -> None:
            tok = hooks.install_all(writer=wa, parent_span_id="a")
            owner_in.set()
            assert part_in.wait(_TIMEOUT)
            hooks.uninstall_all(tok)
            owner_out.set()

        t = threading.Thread(target=_owner)
        t.start()
        assert owner_in.wait(_TIMEOUT)
        tb = hooks.install_all(writer=wb, parent_span_id="b")
        assert tb.startswith(hooks._PARTICIPANT_PREFIX)
        part_in.set()
        assert owner_out.wait(_TIMEOUT)
        t.join(_TIMEOUT)
        state_b = hooks.wire_capture_state(tb)
        hooks.uninstall_all(tb)

        # Capture C runs later in the participant's own thread and wins the race.
        tc = hooks.install_all(writer=wc, parent_span_id="c")
        try:
            assert tc.startswith(hooks._OWNER_PREFIX)
            _fire(_layer(wc))
            rec = get_current_recorder()
            assert rec is not None and rec._capsule_dir.name == "run-c"
        finally:
            hooks.uninstall_all(tc)

        assert _model_calls(tmp_path, "run-c") == 1
        assert _model_calls(tmp_path, "run-b") == 0, "C's call was filed into B"
        # AC3: B's stream has a gap, and its marker says so.
        assert state_b == "scoped-truncated"

    def test_asyncio_participant_in_a_persistent_context(self, tmp_path: Path) -> None:
        """The participant is the main coroutine, whose context outlives it."""
        wa, wb, wc = (_writer(tmp_path, n) for n in ("run-a", "run-b", "run-c"))

        async def _main() -> str:
            owner_in, part_in = asyncio.Event(), asyncio.Event()

            async def owner() -> None:
                tok = hooks.install_all(writer=wa, parent_span_id="a")
                owner_in.set()
                await part_in.wait()
                hooks.uninstall_all(tok)

            task = asyncio.create_task(owner())
            await owner_in.wait()
            tb = hooks.install_all(writer=wb, parent_span_id="b")
            part_in.set()
            await task
            state = hooks.wire_capture_state(tb)
            hooks.uninstall_all(tb)

            tc = hooks.install_all(writer=wc, parent_span_id="c")
            try:
                _fire(_layer(wc))
            finally:
                hooks.uninstall_all(tc)
            return state

        assert asyncio.run(_main()) == "scoped-truncated"
        assert _model_calls(tmp_path, "run-c") == 1
        assert _model_calls(tmp_path, "run-b") == 0


# --------------------------------------------------------------------------- #
# AC3 — a participant that outlives its owner keeps its own capsule
# --------------------------------------------------------------------------- #


def test_participant_outliving_owner_is_not_redirected_to_the_next_owner(
    tmp_path: Path,
) -> None:
    wa, wb, wd = (_writer(tmp_path, n) for n in ("run-a", "run-b", "run-d"))
    ta = hooks.install_all(writer=wa, parent_span_id="a")
    tb_box: list[str] = []
    b_in, a_out, d_in, b_fired = (threading.Event() for _ in range(4))
    td_box: list[str] = []
    d_layer: list[object] = []

    def _participant() -> None:
        tb_box.append(hooks.install_all(writer=wb, parent_span_id="b"))
        b_in.set()
        assert d_in.wait(_TIMEOUT)
        _fire(d_layer[0])  # through D's layer, in B's context
        b_fired.set()

    def _next_owner() -> None:
        td_box.append(hooks.install_all(writer=wd, parent_span_id="d"))
        d_layer.append(_layer(wd))
        d_in.set()
        assert b_fired.wait(_TIMEOUT)
        _fire(d_layer[0])  # D's own call, in D's context

    tp = threading.Thread(target=_participant)
    tp.start()
    assert b_in.wait(_TIMEOUT)
    hooks.uninstall_all(ta)  # the owner finishes first
    a_out.set()
    _run_in_thread(_next_owner)
    tp.join(_TIMEOUT)

    tb, td = tb_box[0], td_box[0]
    assert hooks.wire_capture_state(tb) == "scoped-truncated"
    # D started while B was still bound: B's bare threads could reach D's writer.
    assert hooks.wire_capture_state(td) == "installed-contended"
    hooks.uninstall_all(tb)
    hooks.uninstall_all(td)

    assert _model_calls(tmp_path, "run-b") == 1, "B's call was redirected to D"
    assert _model_calls(tmp_path, "run-d") == 1


def test_a_participant_that_finishes_inside_the_owner_stays_scoped_concurrent(
    tmp_path: Path,
) -> None:
    wa, wb = _writer(tmp_path, "run-a"), _writer(tmp_path, "run-b")
    ta = hooks.install_all(writer=wa, parent_span_id="a")
    states: list[str] = []

    def _participant() -> None:
        tb = hooks.install_all(writer=wb, parent_span_id="b")
        states.append(hooks.wire_capture_state(tb))
        hooks.uninstall_all(tb)

    _run_in_thread(_participant)
    hooks.uninstall_all(ta)
    assert states == ["scoped-concurrent"]


# --------------------------------------------------------------------------- #
# AC4 — an owner nested inside a still-bound capture files into itself
# --------------------------------------------------------------------------- #


def test_owner_nested_inside_a_bound_capture_files_into_itself(tmp_path: Path) -> None:
    wa, wb, wc = (_writer(tmp_path, n) for n in ("run-a", "run-b", "run-c"))
    ta = hooks.install_all(writer=wa, parent_span_id="a")
    tb = hooks.install_all(writer=wb, parent_span_id="b")  # participant, this task
    hooks.uninstall_all(ta)  # owner leaves; B is still running here

    tc = hooks.install_all(writer=wc, parent_span_id="c")  # nested, wins the race
    try:
        assert tc.startswith(hooks._OWNER_PREFIX)
        _fire(_layer(wc))
    finally:
        hooks.uninstall_all(tc)
    _fire(_layer(wc))  # back in B after C releases: B's again
    hooks.uninstall_all(tb)

    assert _model_calls(tmp_path, "run-c") == 1, "nested owner's call went to B"
    assert _model_calls(tmp_path, "run-b") == 1


# --------------------------------------------------------------------------- #
# AC5 — two captures running at once: no cross-talk, in either direction
# --------------------------------------------------------------------------- #

_CALLS = 20


def test_two_thread_captures_interleaved_do_not_cross_file(tmp_path: Path) -> None:
    wa, wb = _writer(tmp_path, "run-a"), _writer(tmp_path, "run-b")
    layer = _layer(wa)  # one patch layer, owned by A
    barrier = threading.Barrier(2, timeout=_TIMEOUT)
    states: dict[str, str] = {}
    tokens: dict[str, str] = {}
    a_first = threading.Event()

    def _capture(name: str, writer: CapsuleWriter) -> None:
        if name == "b":
            assert a_first.wait(_TIMEOUT)
        tokens[name] = hooks.install_all(writer=writer, parent_span_id=name)
        if name == "a":
            a_first.set()
        barrier.wait()
        for _ in range(_CALLS):
            _fire(layer)
        barrier.wait()
        states[name] = hooks.wire_capture_state(tokens[name])
        barrier.wait()
        hooks.uninstall_all(tokens[name])

    threads = [
        threading.Thread(target=_capture, args=("a", wa)),
        threading.Thread(target=_capture, args=("b", wb)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(_TIMEOUT)

    assert _model_calls(tmp_path, "run-a") == _CALLS
    assert _model_calls(tmp_path, "run-b") == _CALLS
    assert states == {"a": "installed-contended", "b": "scoped-concurrent"}


def test_two_asyncio_task_captures_interleaved_do_not_cross_file(
    tmp_path: Path,
) -> None:
    wa, wb = _writer(tmp_path, "run-a"), _writer(tmp_path, "run-b")
    layer = _layer(wa)

    async def _main() -> dict[str, str]:
        states: dict[str, str] = {}
        a_in = asyncio.Event()

        async def capture(name: str, writer: CapsuleWriter) -> None:
            if name == "b":
                await a_in.wait()
            tok = hooks.install_all(writer=writer, parent_span_id=name)
            a_in.set()
            for _ in range(_CALLS):
                _fire(layer)
                await asyncio.sleep(0)  # force interleaving
            states[name] = hooks.wire_capture_state(tok)
            await asyncio.sleep(0)
            hooks.uninstall_all(tok)

        await asyncio.gather(capture("a", wa), capture("b", wb))
        return states

    states = asyncio.run(_main())
    assert _model_calls(tmp_path, "run-a") == _CALLS
    assert _model_calls(tmp_path, "run-b") == _CALLS
    assert states["b"] == "scoped-concurrent"
    assert states["a"] == "installed-contended"


# --------------------------------------------------------------------------- #
# AC6 — bounded bookkeeping and honest degradation
# --------------------------------------------------------------------------- #


def test_bookkeeping_is_empty_once_every_capture_has_released(tmp_path: Path) -> None:
    wa, wb = _writer(tmp_path, "run-a"), _writer(tmp_path, "run-b")
    ta = hooks.install_all(writer=wa, parent_span_id="a")
    tb_box: list[str] = []
    _run_in_thread(lambda: tb_box.append(hooks.install_all(writer=wb, parent_span_id="b")))
    hooks.uninstall_all(ta)
    hooks.uninstall_all(tb_box[0])  # released from a context other than its own

    assert hooks._scope_bindings == {}
    assert hooks._truncated_participants == set()
    assert hooks._contended_owners == set()
    assert er._bindings == {}
    assert hooks.current_hook_owner() is None


def test_legacy_teardown_revokes_a_participant_that_never_released(
    tmp_path: Path,
) -> None:
    wa, wb = _writer(tmp_path, "run-a"), _writer(tmp_path, "run-b")
    hooks.install_all(writer=wa, parent_span_id="a")
    hooks.install_all(writer=wb, parent_span_id="b")  # token dropped on the floor
    assert get_current_writer(None) is wb
    hooks.uninstall_all()
    assert get_current_writer(None) is None
    assert hooks._scope_bindings == {} and er._bindings == {}


def test_a_participant_that_cannot_bind_says_skipped_not_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wa, wb = _writer(tmp_path, "run-a"), _writer(tmp_path, "run-b")
    ta = hooks.install_all(writer=wa, parent_span_id="a")
    monkeypatch.setattr(hooks, "_MAX_LIVE_SCOPES", len(hooks._scope_bindings))
    tb = hooks.install_all(writer=wb, parent_span_id="b")
    try:
        assert tb.startswith(hooks._PARTICIPANT_PREFIX)
        assert hooks.wire_capture_state(tb) == "skipped-concurrent"
        assert get_current_writer(None) is wa  # still the owner's: honestly absent
    finally:
        hooks.uninstall_all(tb)
        hooks.uninstall_all(ta)


def test_a_binding_failure_never_reaches_the_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**_kw: object) -> str:
        raise RuntimeError("bind failed")

    monkeypatch.setattr(er, "bind_capture", _boom)
    wa, wb = _writer(tmp_path, "run-a"), _writer(tmp_path, "run-b")
    ta = hooks.install_all(writer=wa, parent_span_id="a")
    tb = hooks.install_all(writer=wb, parent_span_id="b")
    try:
        assert hooks.wire_capture_state(tb) == "skipped-concurrent"
    finally:
        hooks.uninstall_all(tb)
        hooks.uninstall_all(ta)
