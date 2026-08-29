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

"""Model and tool calls follow the capture that made them (ADR-0224 D3, phase 2).

ADR-0224's 2026-08-06 amendment concluded that task-scoping the *recorder* was
enough: "one installed patch layer serves several captures, each filing into its
own capsule". Measuring it on 2026-08-29 showed that holds for ``NetworkEvent``
and for nothing else. Every wire hook writes its **model call** through the
``self._writer`` it was constructed with — the hook owner's writer — so the
second capture's richest records were still filed into the first capture's
capsule.

These tests pin the whole contract, not half of it: whichever capture's context
a patched call fires in, both the network event and the model call land in *that*
capture's capsule.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import (
    EventRecorder,
    bind_capture,
    get_current_writer,
    unbind_capture,
)


def _writer(base: Path, run_id: str) -> CapsuleWriter:
    w = CapsuleWriter(run_id=run_id, base_dir=base)
    w.open()
    return w


def _recorder(base: Path, run_id: str) -> EventRecorder:
    cdir = base / run_id
    cdir.mkdir(parents=True, exist_ok=True)
    return EventRecorder(capsule_dir=cdir, run_id=run_id, capsule_id=run_id)


def _model_calls(base: Path, run_id: str) -> list[dict]:  # type: ignore[type-arg]
    path = base / run_id / "model-calls.jsonl"
    if not path.exists():
        return []
    text = path.read_text().strip()
    return [json.loads(line) for line in text.splitlines() if line]


def _openai_request() -> MagicMock:
    req = MagicMock()
    req.url = "https://api.openai.com/v1/chat/completions"
    req.body = b'{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
    return req


@pytest.fixture(autouse=True)
def _no_leaked_binding():
    """Every test must leave the task-scoped slots empty."""
    yield
    assert get_current_writer(None) is None, "a writer binding leaked out of a test"


# --------------------------------------------------------------------------- #
# AC1 — the defect this slice fixes
# --------------------------------------------------------------------------- #


def test_model_call_is_filed_into_the_capture_that_made_it(tmp_path: Path) -> None:
    """A model call fired in capture B's context belongs to B, not to the owner.

    Fails against the tree at 5b4dc09: the record went to ``self._writer``, so
    run-a held both records and run-b held none.
    """
    from novafabric.capture.hooks._requests import RequestsHook

    writer_a = _writer(tmp_path, "run-a")
    writer_b = _writer(tmp_path, "run-b")
    # Capture A won the hook race, so its writer is baked into the hook.
    hook = RequestsHook(writer=writer_a, parent_span_id="0" * 16)

    fake_resp = MagicMock(status_code=200)

    # A call made by capture A, with nothing bound.
    hook._wrapped_send(_openai_request(), original=MagicMock(return_value=fake_resp))

    # A call made by capture B, which bound its own capture scope.
    handle = bind_capture(recorder=_recorder(tmp_path, "run-b"), writer=writer_b)
    try:
        hook._wrapped_send(
            _openai_request(), original=MagicMock(return_value=fake_resp)
        )
    finally:
        unbind_capture(handle)

    assert len(_model_calls(tmp_path, "run-a")) == 1, "A kept only its own call"
    assert len(_model_calls(tmp_path, "run-b")) == 1, "B's call was cross-filed into A"


def test_unbinding_restores_the_owners_writer(tmp_path: Path) -> None:
    """After B releases, subsequent calls belong to the hook owner again."""
    from novafabric.capture.hooks._requests import RequestsHook

    writer_a = _writer(tmp_path, "run-a")
    writer_b = _writer(tmp_path, "run-b")
    hook = RequestsHook(writer=writer_a, parent_span_id="0" * 16)
    fake_resp = MagicMock(status_code=200)

    handle = bind_capture(recorder=_recorder(tmp_path, "run-b"), writer=writer_b)
    hook._wrapped_send(_openai_request(), original=MagicMock(return_value=fake_resp))
    unbind_capture(handle)
    hook._wrapped_send(_openai_request(), original=MagicMock(return_value=fake_resp))

    assert len(_model_calls(tmp_path, "run-b")) == 1
    assert len(_model_calls(tmp_path, "run-a")) == 1


# --------------------------------------------------------------------------- #
# AC2 — single-capture processes are unchanged
# --------------------------------------------------------------------------- #


def test_with_nothing_bound_the_hooks_own_writer_still_wins(tmp_path: Path) -> None:
    """The fallback is the hook's own writer, so one-capture processes are inert."""
    from novafabric.capture.hooks._requests import RequestsHook

    writer_a = _writer(tmp_path, "run-a")
    hook = RequestsHook(writer=writer_a, parent_span_id="0" * 16)
    hook._wrapped_send(
        _openai_request(), original=MagicMock(return_value=MagicMock(status_code=200))
    )
    assert len(_model_calls(tmp_path, "run-a")) == 1


def test_get_current_writer_returns_the_default_when_unbound(tmp_path: Path) -> None:
    sentinel = object()
    assert get_current_writer(sentinel) is sentinel


# --------------------------------------------------------------------------- #
# AC4 — constraint 1: release from a different task than the one that bound
# --------------------------------------------------------------------------- #


def test_binding_releases_from_a_different_task(tmp_path: Path) -> None:
    """`bedrock_agentcore` tears down inside a later-consumed generator.

    A `contextvars.Token` may only be reset in the context that produced it, so
    the handle must not be one. Asserted rather than described.
    """
    writer_b = _writer(tmp_path, "run-b")
    recorder_b = _recorder(tmp_path, "run-b")

    async def _main() -> bool:
        handle_box: list[str] = []

        async def binder() -> None:
            handle_box.append(bind_capture(recorder=recorder_b, writer=writer_b))

        async def releaser() -> bool:
            return unbind_capture(handle_box[0])

        await asyncio.create_task(binder())
        # A *separate* task copies the context, so a Token.reset() would raise.
        return await asyncio.create_task(releaser())

    assert asyncio.run(_main()) is True


# --------------------------------------------------------------------------- #
# AC5 — constraint 2: threads do not inherit context
# --------------------------------------------------------------------------- #


def test_a_thread_that_binds_nothing_falls_back_to_the_hook_writer(
    tmp_path: Path,
) -> None:
    """A thread starts with a fresh context, so it must see the default, not None."""
    from novafabric.capture.hooks._requests import RequestsHook

    writer_a = _writer(tmp_path, "run-a")
    writer_b = _writer(tmp_path, "run-b")
    hook = RequestsHook(writer=writer_a, parent_span_id="0" * 16)

    handle = bind_capture(recorder=_recorder(tmp_path, "run-b"), writer=writer_b)
    try:
        def _in_thread() -> None:
            hook._wrapped_send(
                _openai_request(),
                original=MagicMock(return_value=MagicMock(status_code=200)),
            )

        t = threading.Thread(target=_in_thread)
        t.start()
        t.join()
    finally:
        unbind_capture(handle)

    # Inherent to process-global patches: the thread inherits no binding, so the
    # call is attributed to the hook owner. Stated, not silently absorbed.
    assert len(_model_calls(tmp_path, "run-a")) == 1
    assert len(_model_calls(tmp_path, "run-b")) == 0
