"""Google ADK adapter: one plugin, many invocations, each its own capsule.

A ``Runner`` holds **one** plugin instance and serves every invocation through
it, concurrently when the caller gathers several ``run_async`` calls. The
adapter kept its in-flight capture on ``self`` — writer, run id, hook token — so
a second invocation overwrote the first's, the first ``after_run_callback``
finalised the second's capsule, and the owner token was dropped without being
released: the hooks stayed installed for the life of the process.

The real-``Runner`` cases additionally pin the contract ADK 2.x actually calls:
plugins are registered by ``.name`` and every callback is invoked by keyword
(``invocation_context=``). They skip only when ``google-adk`` is absent.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from novafabric.capture import hooks
from novafabric.capture.event_recorder import get_current_writer


@pytest.fixture(autouse=True)
def _clean_state():
    hooks.uninstall_all()
    hooks._contended_owners.clear()
    yield
    owner = hooks.current_hook_owner()
    hooks.uninstall_all()
    hooks._contended_owners.clear()
    assert owner is None, "an ADK capture left the hooks installed"


@pytest.fixture
def _fast_finalise():
    with (
        patch("novafabric.adapters.google_adk.capture_environment", return_value={}),
        patch("novafabric.adapters.google_adk.SecretScannerV0") as scanner,
    ):
        scanner.return_value.scan_and_redact.return_value = {}
        yield


def _manifests(base: Path) -> list[dict]:  # type: ignore[type-arg]
    return [yaml.safe_load(p.read_text()) for p in sorted(base.rglob("capsule.yaml"))]


def _ctx(invocation_id: str) -> SimpleNamespace:
    return SimpleNamespace(invocation_id=invocation_id)


# --------------------------------------------------------------------------- #
# Plugin-level contract (no google-adk needed)
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("_fast_finalise")
def test_interleaved_invocations_each_get_their_own_capsule(tmp_path: Path) -> None:
    from novafabric.adapters.google_adk import NovaAdkPlugin

    plugin = NovaAdkPlugin(tmp_path)
    a, b = _ctx("inv-a"), _ctx("inv-b")

    async def scenario() -> None:
        await plugin.before_run_callback(invocation_context=a)
        await plugin.before_run_callback(invocation_context=b)
        await plugin.after_run_callback(invocation_context=a)
        await plugin.after_run_callback(invocation_context=b)

    asyncio.run(scenario())

    manifests = _manifests(tmp_path)
    assert len(manifests) == 2, "one invocation's capsule was lost"
    assert {m["metadata"]["adk_invocation_id"] for m in manifests} == {"inv-a", "inv-b"}
    assert hooks.current_hook_owner() is None, "the owner token was never released"


def _writer_name() -> str | None:
    writer = get_current_writer(None)
    return None if writer is None else str(writer.capsule_dir.name)


def _writer_name_in_a_thread() -> str | None:
    """Resolve the writer in a fresh ``threading.Thread``; re-raise its failure.

    A bare ``Thread`` swallows its exception (pytest only warns), which is how
    this test once passed with its thread half never running.
    """
    result: list[str | None] = []
    errors: list[BaseException] = []

    def body() -> None:
        try:
            result.append(_writer_name())
        except BaseException as exc:  # noqa: BLE001 — re-raised in the caller
            errors.append(exc)

    t = threading.Thread(target=body)
    t.start()
    t.join(10)
    assert not t.is_alive(), "the probe thread did not finish"
    if errors:
        raise errors[0]
    assert len(result) == 1, "the probe thread produced no result"
    return result[0]


@pytest.mark.usefixtures("_fast_finalise")
def test_concurrent_invocations_resolve_their_own_writer(tmp_path: Path) -> None:
    """Work done inside each invocation — in its task and in a thread it
    starts — resolves that invocation's capsule writer.

    Both invocations sample while **both** are live. Without the barriers the
    first one (the hook owner) can finish first, and its teardown removes the
    patch layer — ``Thread.start`` scope propagation included — before the
    second samples; that degradation is documented and pinned separately below.
    """
    from novafabric.adapters.google_adk import NovaAdkPlugin

    plugin = NovaAdkPlugin(tmp_path)
    task_seen: dict[str, str | None] = {}
    thread_seen: dict[str, str | None] = {}
    both_bound, both_sampled = asyncio.Event(), asyncio.Event()
    bound: set[str] = set()
    sampled: set[str] = set()

    async def invocation(inv: str) -> None:
        ctx = _ctx(inv)
        await plugin.before_run_callback(invocation_context=ctx)
        bound.add(inv)
        if len(bound) == 2:
            both_bound.set()
        await asyncio.wait_for(both_bound.wait(), 10)
        task_seen[inv] = _writer_name()
        thread_seen[inv] = _writer_name_in_a_thread()
        sampled.add(inv)
        if len(sampled) == 2:
            both_sampled.set()
        await asyncio.wait_for(both_sampled.wait(), 10)
        await plugin.after_run_callback(invocation_context=ctx)

    async def scenario() -> None:
        await asyncio.gather(invocation("inv-a"), invocation("inv-b"))

    asyncio.run(scenario())

    by_inv = {m["metadata"]["adk_invocation_id"]: m["run_id"] for m in _manifests(tmp_path)}
    assert set(by_inv) == {"inv-a", "inv-b"}
    assert by_inv["inv-a"] != by_inv["inv-b"]
    assert task_seen == by_inv, ("task", task_seen, by_inv)
    assert thread_seen == by_inv, ("thread", thread_seen, by_inv)


@pytest.mark.usefixtures("_fast_finalise")
def test_a_participant_outliving_the_owner_is_marked_truncated(tmp_path: Path) -> None:
    """The documented degradation (ADR-0224 D3, ``scoped-truncated``).

    When the hook owner finishes first, its teardown removes the patch layer
    while the participant still runs. The participant keeps its task binding,
    but a thread it starts afterwards is no longer carried into its scope and
    resolves no writer — and its capsule says so instead of claiming full wire
    capture.
    """
    from novafabric.adapters.google_adk import NovaAdkPlugin

    plugin = NovaAdkPlugin(tmp_path)
    a, b = _ctx("inv-a"), _ctx("inv-b")
    seen: dict[str, str | None] = {}

    async def scenario() -> None:
        await plugin.before_run_callback(invocation_context=a)  # owner
        await plugin.before_run_callback(invocation_context=b)  # participant
        await plugin.after_run_callback(invocation_context=a)
        seen["task"] = _writer_name()
        seen["thread"] = _writer_name_in_a_thread()
        await plugin.after_run_callback(invocation_context=b)

    asyncio.run(scenario())

    manifests = {m["metadata"]["adk_invocation_id"]: m for m in _manifests(tmp_path)}
    assert seen == {"task": manifests["inv-b"]["run_id"], "thread": None}
    assert manifests["inv-a"]["metadata"]["wire_capture"] == "installed-contended"
    assert manifests["inv-b"]["metadata"]["wire_capture"] == "scoped-truncated"


@pytest.mark.usefixtures("_fast_finalise")
def test_a_failed_invocation_still_releases_the_hooks_and_writes_a_capsule(
    tmp_path: Path,
) -> None:
    """ADK runs after_run only on success; the error path is on_run_error."""
    from novafabric.adapters.google_adk import NovaAdkPlugin

    plugin = NovaAdkPlugin(tmp_path)
    ctx = _ctx("inv-err")

    async def scenario() -> None:
        await plugin.before_run_callback(invocation_context=ctx)
        await plugin.on_run_error_callback(invocation_context=ctx, error=ValueError("boom"))

    asyncio.run(scenario())

    (manifest,) = _manifests(tmp_path)
    assert manifest["status"] == "failure"
    assert manifest["exit_code"] == 1
    assert manifest["error"]["type"] == "ValueError"


def test_a_capture_failure_never_reaches_the_runner(tmp_path: Path) -> None:
    from novafabric.adapters.google_adk import NovaAdkPlugin

    plugin = NovaAdkPlugin(tmp_path)
    ctx = _ctx("inv-x")

    async def scenario() -> None:
        with patch("novafabric.capture.capsule.CapsuleWriter.open", side_effect=OSError("disk")):
            await plugin.before_run_callback(invocation_context=ctx)
        await plugin.after_run_callback(invocation_context=ctx)
        await plugin.on_run_error_callback(invocation_context=ctx, error=RuntimeError("x"))

    asyncio.run(scenario())  # must not raise
    assert _manifests(tmp_path) == []


def test_live_invocations_are_bounded(tmp_path: Path) -> None:
    from novafabric.adapters import google_adk

    plugin = google_adk.NovaAdkPlugin(tmp_path)

    async def scenario() -> None:
        with patch.object(google_adk, "_MAX_LIVE_INVOCATIONS", 2):
            for i in range(3):
                await plugin.before_run_callback(invocation_context=_ctx(f"inv-{i}"))
        assert len(plugin._runs) == 2
        for i in range(3):
            await plugin.on_run_error_callback(
                invocation_context=_ctx(f"inv-{i}"), error=RuntimeError("x")
            )

    with (
        patch("novafabric.adapters.google_adk.capture_environment", return_value={}),
        patch("novafabric.adapters.google_adk.SecretScannerV0") as scanner,
    ):
        scanner.return_value.scan_and_redact.return_value = {}
        asyncio.run(scenario())
    assert plugin._runs == {}


# --------------------------------------------------------------------------- #
# Against a real google-adk Runner
# --------------------------------------------------------------------------- #


def _echo_agent_cls():  # type: ignore[no-untyped-def]
    from google.adk.agents import BaseAgent
    from google.adk.events import Event
    from google.genai import types

    class _Echo(BaseAgent):
        async def _run_async_impl(self, ctx):  # type: ignore[no-untyped-def,override]
            await asyncio.sleep(0.05)
            writer = get_current_writer(None)
            name = writer.capsule_dir.name if writer is not None else "<none>"
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(text=name)]),
            )

    return _Echo


@pytest.mark.usefixtures("_fast_finalise")
def test_real_runner_concurrent_invocations_file_into_their_own_capsules(
    tmp_path: Path,
) -> None:
    pytest.importorskip("google.adk")
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from novafabric.adapters.google_adk import make_plugin

    svc = InMemorySessionService()
    runner = Runner(
        app_name="nova-test",
        agent=_echo_agent_cls()(name="echo"),
        session_service=svc,
        plugins=[make_plugin(tmp_path)],
    )

    async def one() -> tuple[str, str]:
        session = await svc.create_session(app_name="nova-test", user_id="u")
        events = [
            e
            async for e in runner.run_async(
                user_id="u",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
            )
        ]
        return events[-1].invocation_id, events[-1].content.parts[0].text

    async def scenario() -> list[tuple[str, str]]:
        return list(await asyncio.gather(one(), one()))

    results = asyncio.run(scenario())

    by_inv = {m["metadata"]["adk_invocation_id"]: m["run_id"] for m in _manifests(tmp_path)}
    assert len(by_inv) == 2
    for invocation_id, resolved_run in results:
        assert by_inv[invocation_id] == resolved_run, (results, by_inv)
