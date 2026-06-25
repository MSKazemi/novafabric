from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter

_installed: list[object] = []

# Tracks whether install_all() set the EventRecorder singleton itself (vs an
# orchestrator that manages its own recorder lifecycle). Only when we set it do
# we clear it in uninstall_all() — so we never clobber an externally-owned one.
_recorder_set_by_install: bool = False


# Map of (target SDK module, hook module path, hook class name).
# Order is significant for the layering guard:
#   - Per-SDK hooks first (richer records than wire-level)
#   - Wire-level higher-layer before lower (requests > urllib3) so the
#     layering guard in _layering.py prevents double-recording
#   - Plugin discovery happens separately, after all built-ins
_BUILT_IN_HOOKS: tuple[tuple[str, str, str], ...] = (
    ("openai",     "novafabric.capture.hooks._openai",     "OpenAIHook"),
    ("anthropic",  "novafabric.capture.hooks._anthropic",  "AnthropicHook"),
    ("mcp",        "novafabric.capture.hooks._mcp",        "MCPHook"),
    ("requests",   "novafabric.capture.hooks._requests",   "RequestsHook"),
    ("httpx",      "novafabric.capture.hooks._httpx",      "HttpxHook"),
    ("aiohttp",    "novafabric.capture.hooks._aiohttp",    "AiohttpHook"),
    ("urllib3",    "novafabric.capture.hooks._urllib3",    "Urllib3Hook"),
)


def _is_sdk_available(sdk_module: str) -> bool:
    """Return True if ``sdk_module`` is importable WITHOUT importing it.

    Uses :func:`importlib.util.find_spec` to probe the import system
    for the module's spec without executing it.

    Originally introduced in v0.6.9 as a hot-path optimization with
    the hypothesis that find_spec would be substantially cheaper than
    full hook-module imports for absent SDKs. **The hypothesis was
    measured and disproven**: find_spec walks the same import-system
    machinery as a real import, so for the small hook modules in this
    package the saving is ~1.5 ms — within benchmark noise. See
    ``benchmarks/README.md`` for the methodology.

    The check is kept anyway because it makes intent explicit (check
    then import, vs try-then-recover) and avoids loading hook modules
    into ``sys.modules`` when they would no-op. Real subprocess-
    startup wins now require either a leaner novafabric package init
    chain or a sys.audit / sys.meta_path-based deferred install — both
    queued for v0.7.x architectural work.
    """
    try:
        return importlib.util.find_spec(sdk_module) is not None
    except (ImportError, ValueError):
        # ImportError: parent package is broken; ValueError: __spec__ is None.
        # Either way, the SDK isn't usable — treat as absent.
        return False


def _ensure_recorder(writer: "CapsuleWriter") -> None:
    """Set the module-level EventRecorder singleton from *writer* if unset.

    Wire-level hooks record NetworkEvent/FileEvent via
    ``get_current_recorder()`` rather than holding the writer directly, so the
    singleton must exist before any patched method can fire an event. This is
    essential wherever the hooks run in a different process or scope than the
    orchestrator that owns the recorder — the captured-workload subprocess
    (sitecustomize loader), the in-process SDK ``capture()`` wrapper, and the
    framework adapters. Without it the recorder is ``None`` in those contexts
    and all network/file events are silently dropped.

    Idempotent and fail-open: only sets the recorder when none exists (so an
    externally-owned recorder is never clobbered) and never lets a setup
    failure block hook installation.
    """
    global _recorder_set_by_install
    try:
        from novafabric.capture.event_recorder import (
            EventRecorder,
            get_current_recorder,
            set_current_recorder,
        )
        if get_current_recorder() is None:
            _cap_dir = writer.capsule_dir
            set_current_recorder(EventRecorder(
                capsule_dir=_cap_dir,
                run_id=_cap_dir.name,
                capsule_id=_cap_dir.name,
            ))
            _recorder_set_by_install = True
    except Exception:
        pass  # fail-open: recorder is best-effort; never block capture


def _install_plugins(writer: "CapsuleWriter", parent_span_id: str) -> None:
    """Discover and install third-party hook plugins (entry-point group).

    Plugins discovered via the 'novafabric.hooks' entry-point group (RFC-0001
    §Detailed design — Option C, sub-track C-2). Each plugin is isolated: a
    buggy plugin must not break built-in capture. Plugin discovery is NOT
    skipped based on SDK availability — plugin authors opt in via the entry
    point and may have their own dependency-presence checks.
    """
    from novafabric.capture.hooks._plugin import install_discovered_plugins
    _installed.extend(
        install_discovered_plugins(writer=writer, parent_span_id=parent_span_id)
    )


def install_all(writer: "CapsuleWriter", parent_span_id: str) -> None:
    """Install every built-in hook whose target SDK is importable.

    Hooks for absent SDKs are skipped entirely — the hook module is
    not even imported. The performance impact is small (see
    :func:`_is_sdk_available`); the main benefit is making intent
    explicit at the install-orchestration layer.

    Sets the EventRecorder singleton (see :func:`_ensure_recorder`) so the
    wire-level hooks can write their event streams.

    This is the **eager** path: present SDKs are imported now in order to be
    patched. For the import-deferred path that avoids importing unused SDKs at
    startup, see :func:`install_all_deferred` (``--fast-emit``).
    """
    _ensure_recorder(writer)

    for sdk_module, hook_module_path, hook_class_name in _BUILT_IN_HOOKS:
        if not _is_sdk_available(sdk_module):
            continue
        try:
            hook_module = importlib.import_module(hook_module_path)
            hook_cls = getattr(hook_module, hook_class_name)
        except (ImportError, AttributeError):
            # Defensive: if the hook module itself is broken (shouldn't
            # happen with built-ins, but possible in dev), skip it
            # rather than poison the rest of capture.
            continue
        hook = hook_cls(writer=writer, parent_span_id=parent_span_id)
        hook.install()
        _installed.append(hook)

    _install_plugins(writer, parent_span_id)


def install_all_deferred(writer: "CapsuleWriter", parent_span_id: str) -> None:
    """Install built-in hooks *lazily*, triggered by the workload's own imports.

    Unlike :func:`install_all`, this does **not** import any target SDK at
    startup. For each built-in hook it registers a one-shot ``sys.meta_path``
    post-import callback (see :mod:`novafabric.capture.hooks._deferred`) that
    fires only if/when the workload imports that SDK — at which point the SDK
    is already loaded (the workload's own cost) and patching adds ~nothing.

    This eliminates "startup cost #2" — the ~700 ms (openai) / ~340 ms (mcp)
    paid at interpreter startup by the eager path to import SDKs purely to patch
    them, even when the workload never calls them. It is the lazy-import path of
    the warm-capture-daemon design (ADR-0092 slice B).

    Fidelity is preserved: when a hook *does* install (its SDK is imported), it
    is the identical hook class, and the EventRecorder singleton is set lazily
    in the callback before the patched method can fire an event. SDKs already
    imported when this runs are patched immediately (handled by
    :func:`~novafabric.capture.hooks._deferred.register_post_import_hook`).
    Plugin discovery still runs eagerly (small, opt-in, no uniform target SDK
    to gate on).
    """
    from novafabric.capture.hooks._deferred import register_post_import_hook

    def _make_callback(hook_module_path: str, hook_class_name: str) -> object:
        def _on_import(_module: object) -> None:
            # Recorder must exist before the patched method can fire an event.
            _ensure_recorder(writer)
            try:
                hook_module = importlib.import_module(hook_module_path)
                hook_cls = getattr(hook_module, hook_class_name)
            except (ImportError, AttributeError):
                return
            hook = hook_cls(writer=writer, parent_span_id=parent_span_id)
            hook.install()
            _installed.append(hook)
        return _on_import

    for sdk_module, hook_module_path, hook_class_name in _BUILT_IN_HOOKS:
        register_post_import_hook(
            sdk_module,
            _make_callback(hook_module_path, hook_class_name),  # type: ignore[arg-type]
        )

    _install_plugins(writer, parent_span_id)


def uninstall_all() -> None:
    global _recorder_set_by_install
    for hook in _installed:
        try:
            hook.uninstall()  # type: ignore[attr-defined]
        except Exception:
            pass
    _installed.clear()
    # Clear the recorder only if install_all() set it (in-process SDK / adapter
    # paths). When an orchestrator owns the recorder it clears its own.
    if _recorder_set_by_install:
        try:
            from novafabric.capture.event_recorder import set_current_recorder
            set_current_recorder(None)
        except Exception:
            pass
        finally:
            _recorder_set_by_install = False
