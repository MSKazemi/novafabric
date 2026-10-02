"""Strict capture mode: refuse overlapping in-process captures (ADR-0224 OQ-2).

Opt-in. Default behaviour (scoped participation) is pinned by
``test_concurrent_capture_scopes.py`` and must not change.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from novafabric.capture import hooks
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import get_current_writer


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(hooks.STRICT_ENV_VAR, raising=False)
    hooks.uninstall_all()
    hooks._contended_owners.clear()
    yield
    leaked = get_current_writer(None)
    hooks.uninstall_all()
    hooks._contended_owners.clear()
    assert leaked is None


def _w(base: Path, rid: str) -> CapsuleWriter:
    w = CapsuleWriter(run_id=rid, base_dir=base)
    w.open()
    return w


def test_default_is_unchanged_a_second_capture_participates(tmp_path: Path) -> None:
    a = hooks.install_all(writer=_w(tmp_path, "A"), parent_span_id="a" * 16)
    b = hooks.install_all(writer=_w(tmp_path, "B"), parent_span_id="b" * 16)
    assert b.startswith("par:")
    hooks.uninstall_all(b)
    hooks.uninstall_all(a)


def test_strict_refuses_an_overlapping_capture_and_leaves_no_state(tmp_path: Path) -> None:
    a = hooks.install_all(writer=_w(tmp_path, "A"), parent_span_id="a" * 16)
    bindings, installed = dict(hooks._scope_bindings), list(hooks._installed)
    with pytest.raises(hooks.ConcurrentCaptureRefused, match="strict"):
        hooks.install_all(writer=_w(tmp_path, "B"), parent_span_id="b" * 16, strict=True)
    assert hooks._scope_bindings == bindings
    assert hooks._installed == installed
    assert hooks.current_hook_owner() == a
    # The owner is not marked contended: nothing overlapped it.
    assert hooks.wire_capture_state(a) == "installed"
    hooks.uninstall_all(a)


def test_strict_via_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = hooks.install_all(writer=_w(tmp_path, "A"), parent_span_id="a" * 16)
    monkeypatch.setenv(hooks.STRICT_ENV_VAR, "1")
    with pytest.raises(hooks.ConcurrentCaptureRefused):
        hooks.install_all(writer=_w(tmp_path, "B"), parent_span_id="b" * 16)
    hooks.uninstall_all(a)


def test_explicit_false_overrides_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = hooks.install_all(writer=_w(tmp_path, "A"), parent_span_id="a" * 16)
    monkeypatch.setenv(hooks.STRICT_ENV_VAR, "1")
    b = hooks.install_all(writer=_w(tmp_path, "B"), parent_span_id="b" * 16, strict=False)
    assert b.startswith("par:")
    hooks.uninstall_all(b)
    hooks.uninstall_all(a)


def test_strict_does_not_refuse_sequential_captures(tmp_path: Path) -> None:
    for rid in "AB":
        t = hooks.install_all(writer=_w(tmp_path, rid), parent_span_id="a" * 16, strict=True)
        assert t.startswith("own:")
        hooks.uninstall_all(t)


def test_strict_refuses_an_owner_while_an_orphaned_participant_is_live(tmp_path: Path) -> None:
    a = hooks.install_all(writer=_w(tmp_path, "A"), parent_span_id="a" * 16)
    b = hooks.install_all(writer=_w(tmp_path, "B"), parent_span_id="b" * 16)
    hooks.uninstall_all(a)  # owner done first; B still running
    with pytest.raises(hooks.ConcurrentCaptureRefused):
        hooks.install_all(writer=_w(tmp_path, "C"), parent_span_id="c" * 16, strict=True)
    hooks.uninstall_all(b)


def test_adk_plugin_propagates_the_refusal(tmp_path: Path) -> None:
    from novafabric.adapters.google_adk import NovaAdkPlugin

    plugin = NovaAdkPlugin(tmp_path)
    a = hooks.install_all(writer=_w(tmp_path, "A"), parent_span_id="a" * 16)

    async def go() -> None:
        with patch.dict("os.environ", {hooks.STRICT_ENV_VAR: "1"}):
            await plugin.before_run_callback(invocation_context=SimpleNamespace(invocation_id="x"))

    with pytest.raises(hooks.ConcurrentCaptureRefused):
        asyncio.run(go())
    assert plugin._runs == {}
    hooks.uninstall_all(a)
