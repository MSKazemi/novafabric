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

"""A recorder can be bound per task, not just per process (ADR-0224 D3, phase 2).

Phase 1 stopped concurrent in-process captures from corrupting each other by
letting exactly one of them own the hooks. The loser records no wire events at
all — a stated limitation, not a fixed problem.

This is the foundation that lifts it. ``get_current_recorder()`` resolves a
task-bound recorder before the process-wide singleton, and the hooks call it
when an event *fires*, so one installed set of hooks can serve several captures
at once with each filing into its own capsule.

Both constraints ADR-0224 D3 recorded are asserted here, because both are the
kind that look fine until the exact case that breaks them:

* a binding must be releasable from a **different task** than the one that made
  it (`bedrock_agentcore` tears down inside a later-consumed generator), which
  is why the handle is not a `contextvars.Token`;
* **threads do not inherit context**, so a thread that binds nothing must still
  see the singleton rather than nothing at all.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from novafabric.capture.event_recorder import (
    EventRecorder,
    bind_recorder,
    clear_current_recorder,
    get_current_recorder,
    set_current_recorder,
    unbind_recorder,
)


def _recorder(tmp_path: Path, name: str) -> EventRecorder:
    cdir = tmp_path / name
    cdir.mkdir(parents=True, exist_ok=True)
    return EventRecorder(capsule_dir=cdir, run_id=name, capsule_id=name)


@pytest.fixture(autouse=True)
def _clean_singleton():
    """The singleton is process-wide; leaving one set would leak across tests."""
    set_current_recorder(None)
    yield
    set_current_recorder(None)


class TestResolution:
    def test_singleton_answers_when_nothing_is_bound(self, tmp_path: Path) -> None:
        rec = _recorder(tmp_path, "solo")
        set_current_recorder(rec)
        assert get_current_recorder() is rec

    def test_binding_wins_over_the_singleton(self, tmp_path: Path) -> None:
        singleton = _recorder(tmp_path, "singleton")
        bound = _recorder(tmp_path, "bound")
        set_current_recorder(singleton)
        handle = bind_recorder(bound)
        try:
            assert get_current_recorder() is bound
        finally:
            unbind_recorder(handle)
        assert get_current_recorder() is singleton, "release must restore the fallback"

    def test_no_recorder_at_all_is_none(self) -> None:
        assert get_current_recorder() is None


class TestConcurrentCaptures:
    """The point of the whole exercise: two captures, two capsules, one process."""

    def test_two_async_tasks_each_see_their_own_recorder(
        self, tmp_path: Path
    ) -> None:
        a = _recorder(tmp_path, "capture-a")
        b = _recorder(tmp_path, "capture-b")
        seen: dict[str, list[EventRecorder | None]] = {"a": [], "b": []}

        async def capture(name: str, rec: EventRecorder) -> None:
            handle = bind_recorder(rec)
            try:
                for _ in range(3):
                    # Yield control so the two captures genuinely interleave;
                    # without this the test would pass on sequencing alone.
                    await asyncio.sleep(0)
                    seen[name].append(get_current_recorder())
            finally:
                unbind_recorder(handle)

        async def main() -> None:
            await asyncio.gather(capture("a", a), capture("b", b))

        asyncio.run(main())

        assert seen["a"] == [a, a, a]
        assert seen["b"] == [b, b, b]

    def test_the_loser_of_the_hook_race_still_resolves_its_own_recorder(
        self, tmp_path: Path
    ) -> None:
        """Phase 1's limitation, lifted.

        The capture that loses the hook race installs nothing, so events fire
        through the *winner's* patches. Since those patches resolve the recorder
        at event time, the loser's events still reach the loser's capsule.
        """
        winner = _recorder(tmp_path, "winner")
        loser = _recorder(tmp_path, "loser")
        set_current_recorder(winner)  # as the winning install_all() would

        handle = bind_recorder(loser)
        try:
            # Standing in for a patched method firing inside the loser's task.
            assert get_current_recorder() is loser
        finally:
            unbind_recorder(handle)


class TestNonLexicalRelease:
    """Constraint 1 — the handle is not a ContextVar Token, and this is why."""

    def test_a_binding_can_be_released_from_a_different_task(
        self, tmp_path: Path
    ) -> None:
        """Separate ``Task``s, which is where a Token genuinely breaks.

        The boundary is narrower than it first looks, and worth stating exactly:
        two coroutines merely ``await``-ed in sequence share one task and one
        context, so a Token *would* work there. Each ``create_task`` runs in a
        **copy** of the context, and resetting a Token across that copy raises
        ``ValueError: Token was created in a different Context`` — verified, not
        assumed. That is the shape of the ``bedrock_agentcore`` teardown ADR-0224
        warns about.
        """
        rec = _recorder(tmp_path, "generator-teardown")
        handles: dict[str, str] = {}

        async def binds() -> None:
            handles["h"] = bind_recorder(rec)

        async def releases() -> None:
            assert unbind_recorder(handles["h"]) is True

        async def main() -> None:
            await asyncio.create_task(binds())
            await asyncio.create_task(releases())

        asyncio.run(main())
        assert get_current_recorder() is None

    def test_a_contextvar_token_would_not_survive_this(self) -> None:
        """The negative half of the claim above, asserted rather than asserted-in-prose.

        If a future Python made a Token resettable across tasks, this fails and
        the handle indirection could be reconsidered on evidence.
        """
        from contextvars import ContextVar

        var: ContextVar[str | None] = ContextVar("probe", default=None)
        tokens: dict[str, object] = {}

        async def binds() -> None:
            tokens["t"] = var.set("bound")

        async def releases() -> None:
            with pytest.raises(ValueError, match="different Context"):
                var.reset(tokens["t"])  # type: ignore[arg-type]

        async def main() -> None:
            await asyncio.create_task(binds())
            await asyncio.create_task(releases())

        asyncio.run(main())

    def test_releasing_from_a_plain_thread_also_works(self, tmp_path: Path) -> None:
        rec = _recorder(tmp_path, "cross-thread")
        handle = bind_recorder(rec)
        result: dict[str, bool] = {}

        thread = threading.Thread(target=lambda: result.update(ok=unbind_recorder(handle)))
        thread.start()
        thread.join()

        assert result["ok"] is True

    def test_unbinding_twice_is_a_no_op_not_an_error(self, tmp_path: Path) -> None:
        """A teardown path must never raise into the workload it is capturing."""
        handle = bind_recorder(_recorder(tmp_path, "twice"))
        assert unbind_recorder(handle) is True
        assert unbind_recorder(handle) is False

    def test_unbinding_an_unknown_handle_is_a_no_op(self) -> None:
        assert unbind_recorder("never-issued") is False

    def test_a_released_binding_is_not_retained(self, tmp_path: Path) -> None:
        """The bindings map must not grow across captures."""
        from novafabric.capture import event_recorder

        before = len(event_recorder._bindings)
        for i in range(5):
            unbind_recorder(bind_recorder(_recorder(tmp_path, f"churn-{i}")))
        assert len(event_recorder._bindings) == before


class TestThreadsDoNotInheritContext:
    """Constraint 2 — a thread starts with a fresh context, so it must fall back."""

    def test_a_thread_that_binds_nothing_sees_the_singleton(
        self, tmp_path: Path
    ) -> None:
        singleton = _recorder(tmp_path, "process-wide")
        bound = _recorder(tmp_path, "main-task-only")
        set_current_recorder(singleton)
        handle = bind_recorder(bound)
        seen: dict[str, EventRecorder | None] = {}
        try:
            thread = threading.Thread(
                target=lambda: seen.update(rec=get_current_recorder())
            )
            thread.start()
            thread.join()
        finally:
            unbind_recorder(handle)

        assert seen["rec"] is singleton, (
            "a thread does not inherit the binding, so it must fall back to the "
            "singleton — falling back to None would silently drop every event"
        )

    def test_a_thread_may_bind_its_own(self, tmp_path: Path) -> None:
        own = _recorder(tmp_path, "thread-own")
        seen: dict[str, EventRecorder | None] = {}

        def worker() -> None:
            handle = bind_recorder(own)
            try:
                seen["rec"] = get_current_recorder()
            finally:
                unbind_recorder(handle)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert seen["rec"] is own
        assert get_current_recorder() is None, "the thread's binding must not escape"


class TestBackwardCompatibility:
    def test_nothing_bound_means_todays_behaviour_exactly(
        self, tmp_path: Path
    ) -> None:
        """No caller binds yet, so this slice must be observationally inert."""
        rec = _recorder(tmp_path, "unchanged")
        set_current_recorder(rec)
        assert get_current_recorder() is rec
        set_current_recorder(None)
        assert get_current_recorder() is None


class TestClearIsIdentityGuarded:
    """Run A's teardown must not blank run B's recorder.

    ``CaptureOrchestrator.run()`` brackets a run with
    ``set_current_recorder(rec)`` ... ``set_current_recorder(None)``, and the
    clear is unconditional. That is correct for one run at a time and wrong the
    moment two overlap in one process: the first to finish clears the singleton
    the second is still recording through, and ``record.py`` resolves ``None``
    to a silent no-op because every ``record_*`` path is fail-open. The result
    is a capsule that is missing events and says nothing about it.

    No shipped path runs two orchestrators in one process today — the daemon
    forks per worker, ``run_experiment`` iterates sequentially, and the CLI runs
    one. So this guards a hazard rather than fixing a live outage, which is the
    honest reason it is cheap: identity-compare on clear costs nothing and
    removes a trap from the path the module's own docstring invites callers onto.
    """

    def test_clearing_with_a_foreign_recorder_leaves_the_live_one_alone(
        self, tmp_path: Path
    ) -> None:
        run_a = _recorder(tmp_path, "run-a")
        run_b = _recorder(tmp_path, "run-b")

        set_current_recorder(run_a)
        set_current_recorder(run_b)  # B starts while A is still running

        # A finishes and tears down. It must clear only if it still owns the slot.
        clear_current_recorder(run_a)

        assert get_current_recorder() is run_b, (
            "run A's teardown blanked run B's recorder; every event B records "
            "from here is silently dropped by the fail-open no-op in record.py"
        )

        clear_current_recorder(run_b)
        assert get_current_recorder() is None

    def test_clearing_the_owner_still_clears(self, tmp_path: Path) -> None:
        """The guard must not turn teardown into a leak."""
        rec = _recorder(tmp_path, "solo")
        set_current_recorder(rec)
        clear_current_recorder(rec)
        assert get_current_recorder() is None

    def test_clearing_when_nothing_is_set_is_a_no_op(self, tmp_path: Path) -> None:
        """Teardown runs in a ``finally``; it must never raise into the workload."""
        assert get_current_recorder() is None
        clear_current_recorder(_recorder(tmp_path, "never-set"))
        assert get_current_recorder() is None
