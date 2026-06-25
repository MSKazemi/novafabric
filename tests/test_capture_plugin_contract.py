"""Tests for the third-party plugin contract (RFC-0001 Option C, v0.5.x C-2).

The contract is exercised end-to-end with **fake entry points** injected via
``unittest.mock.patch`` against ``importlib.metadata.entry_points``. No real
PyPI package is needed; the same code path runs in production when a
plugin is ``pip install``-ed.
"""
from __future__ import annotations

import logging
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any
from unittest.mock import patch

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.hooks._plugin import (
    ENTRY_POINT_GROUP,
    HookPluginInfo,
    HookProtocol,
    discover_plugin_hooks,
    install_discovered_plugins,
)

RUN_ID = "01HXTEST000000000000000000"


def _make_writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


# Stand-in plugin classes used as entry-point targets. Real plugins live in
# their own packages; here we attach them to ``__main__``-style modules and
# hand the entry-point loader them directly via patching.
class _GoodHook:
    def __init__(self, writer: Any, parent_span_id: str) -> None:
        self.writer = writer
        self.parent_span_id = parent_span_id
        self.installed = False

    def install(self) -> None:
        self.installed = True

    def uninstall(self) -> None:
        self.installed = False


class _HookWithInfo(_GoodHook):
    @classmethod
    def info(cls) -> HookPluginInfo:
        return HookPluginInfo(
            name="hook-with-info", version="1.2.3", capabilities=("demo",)
        )


class _BrokenInstallHook(_GoodHook):
    def install(self) -> None:
        raise RuntimeError("install boom")


class _BrokenConstructorHook:
    def __init__(self, writer: Any, parent_span_id: str) -> None:
        raise RuntimeError("constructor boom")

    def install(self) -> None: ...
    def uninstall(self) -> None: ...


class _NotAHookClass:
    """Has no install / uninstall — should be rejected by the looks-like
    check before it ever gets instantiated."""


# ---------------------------------------------------------------- Protocol


def test_hook_protocol_accepts_minimal_class() -> None:
    """A class with install + uninstall (and a 2-arg constructor) is a hook."""
    assert isinstance(_GoodHook(writer=None, parent_span_id="0" * 16), HookProtocol)


def test_hook_protocol_rejects_class_missing_uninstall() -> None:
    class _Half:
        def __init__(self, writer: Any, parent_span_id: str) -> None: ...
        def install(self) -> None: ...

    assert not isinstance(_Half(writer=None, parent_span_id="0" * 16), HookProtocol)


# ---------------------------------------------------------------- discovery


def _entry_points(*pairs: tuple[str, type]) -> list[EntryPoint]:
    """Build EntryPoint objects whose ``load()`` returns the given class."""
    out: list[EntryPoint] = []
    for name, cls in pairs:
        ep = EntryPoint(name=name, value=f"{cls.__module__}:{cls.__name__}",
                        group=ENTRY_POINT_GROUP)
        # Replace .load with a lambda binding `cls`. EntryPoint is a NamedTuple
        # so we can't set attributes; wrap via patch instead.
        out.append(ep)
    return out


def test_discover_returns_valid_plugins(monkeypatch: Any) -> None:
    eps = _entry_points(("good", _GoodHook), ("withinfo", _HookWithInfo))

    def _fake_load(self: EntryPoint) -> type:
        return {"good": _GoodHook, "withinfo": _HookWithInfo}[self.name]

    monkeypatch.setattr(EntryPoint, "load", _fake_load)
    with patch(
        "novafabric.capture.hooks._plugin.metadata.entry_points",
        return_value=eps,
    ):
        discovered = discover_plugin_hooks()

    names = {d.info.name for d in discovered}
    # _HookWithInfo.info() returns name="hook-with-info"; _GoodHook falls back
    # to the entry-point name "good".
    assert "good" in names
    assert "hook-with-info" in names


def test_discover_skips_classes_failing_looks_like_hook(
    monkeypatch: Any, caplog: Any
) -> None:
    eps = _entry_points(("incomplete", _NotAHookClass))

    def _fake_load(self: EntryPoint) -> type:
        return _NotAHookClass

    monkeypatch.setattr(EntryPoint, "load", _fake_load)
    caplog.set_level(logging.WARNING, logger="novafabric.capture.hooks._plugin")
    with patch(
        "novafabric.capture.hooks._plugin.metadata.entry_points",
        return_value=eps,
    ):
        discovered = discover_plugin_hooks()
    assert discovered == []
    assert any("HookProtocol" in r.message for r in caplog.records)


def test_discover_skips_failing_imports(monkeypatch: Any, caplog: Any) -> None:
    eps = _entry_points(("explodes", _GoodHook))

    def _fake_load(self: EntryPoint) -> type:
        raise ImportError("module gone")

    monkeypatch.setattr(EntryPoint, "load", _fake_load)
    caplog.set_level(logging.WARNING, logger="novafabric.capture.hooks._plugin")
    with patch(
        "novafabric.capture.hooks._plugin.metadata.entry_points",
        return_value=eps,
    ):
        discovered = discover_plugin_hooks()
    assert discovered == []
    assert any("failed to load" in r.message for r in caplog.records)


def test_discover_handles_entry_point_lookup_failure(caplog: Any) -> None:
    """If `importlib.metadata.entry_points` itself raises, return [] not crash."""
    caplog.set_level(logging.WARNING, logger="novafabric.capture.hooks._plugin")
    with patch(
        "novafabric.capture.hooks._plugin.metadata.entry_points",
        side_effect=RuntimeError("boom"),
    ):
        assert discover_plugin_hooks() == []
    assert any("entry_points lookup failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------- lifecycle


def test_install_isolates_constructor_failures(
    tmp_path: Path, caplog: Any
) -> None:
    from novafabric.capture.hooks._plugin import DiscoveredHook
    plugins = [
        DiscoveredHook(
            info=HookPluginInfo(name="broken-ctor"),
            cls=_BrokenConstructorHook,
            entry_point_value="x",
        ),
        DiscoveredHook(
            info=HookPluginInfo(name="good"),
            cls=_GoodHook,
            entry_point_value="y",
        ),
    ]
    caplog.set_level(logging.WARNING, logger="novafabric.capture.hooks._plugin")
    installed = install_discovered_plugins(
        writer=_make_writer(tmp_path),
        parent_span_id="0" * 16,
        discovered=plugins,
    )
    assert len(installed) == 1
    assert isinstance(installed[0], _GoodHook)
    assert any("broken-ctor" in r.message for r in caplog.records)


def test_install_isolates_install_failures(
    tmp_path: Path, caplog: Any
) -> None:
    from novafabric.capture.hooks._plugin import DiscoveredHook
    plugins = [
        DiscoveredHook(
            info=HookPluginInfo(name="broken-install"),
            cls=_BrokenInstallHook,
            entry_point_value="x",
        ),
        DiscoveredHook(
            info=HookPluginInfo(name="good"),
            cls=_GoodHook,
            entry_point_value="y",
        ),
    ]
    caplog.set_level(logging.WARNING, logger="novafabric.capture.hooks._plugin")
    installed = install_discovered_plugins(
        writer=_make_writer(tmp_path),
        parent_span_id="0" * 16,
        discovered=plugins,
    )
    assert len(installed) == 1
    assert isinstance(installed[0], _GoodHook)
    assert any("broken-install" in r.message for r in caplog.records)


def test_install_all_continues_when_plugin_install_fails(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """The end-to-end guarantee: a buggy plugin must not break built-in capture."""
    from novafabric.capture.hooks import _installed, install_all, uninstall_all
    from novafabric.capture.hooks._plugin import DiscoveredHook

    _installed.clear()

    def _fake_discover() -> list[DiscoveredHook]:
        return [
            DiscoveredHook(
                info=HookPluginInfo(name="bad"),
                cls=_BrokenInstallHook,
                entry_point_value="x",
            )
        ]

    monkeypatch.setattr(
        "novafabric.capture.hooks._plugin.discover_plugin_hooks",
        _fake_discover,
    )
    caplog.set_level(logging.WARNING, logger="novafabric.capture.hooks._plugin")

    writer = _make_writer(tmp_path)
    install_all(writer=writer, parent_span_id="0" * 16)
    try:
        # The contract being tested: a failed plugin does NOT break
        # built-in installs. As of v0.6.9, install_all only loads
        # built-in hooks whose target SDK is importable in the current
        # env (importlib.util.find_spec). httpx is a NovaFabric runtime
        # dep and is always present, so HttpxHook MUST be installed
        # regardless of plugin state.
        builtin_names = {type(h).__name__ for h in _installed}
        assert "HttpxHook" in builtin_names, (
            f"HttpxHook (always-available built-in) not installed; "
            f"plugin failure leaked: {builtin_names}"
        )
        # The broken plugin must NOT be in _installed.
        assert "_BrokenInstallHook" not in builtin_names
        # And the failure must be logged.
        assert any("bad" in r.message for r in caplog.records)
    finally:
        uninstall_all()
