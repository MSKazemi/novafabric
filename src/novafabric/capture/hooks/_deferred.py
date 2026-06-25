"""Deferred, import-triggered hook installation (``--fast-emit``).

The default capture path (:func:`novafabric.capture.hooks.install_all`) runs at
the captured workload's interpreter startup (via the sitecustomize loader) and
*eagerly imports every present target SDK* in order to patch it. Measured cost
(``-X importtime``, this repo): importing ``openai`` to patch it is ~700 ms and
``mcp`` ~340 ms — paid at startup even if the workload never calls those SDKs.
That is "startup cost #2" in the warm-capture-daemon design (slice A removed
cost #1, the orchestrator cold-start; this module removes cost #2).

The fix is a **post-import hook**: instead of importing the SDK to patch it, we
register a one-shot callback on ``sys.meta_path`` that fires *the moment the
workload itself imports the SDK* — and never fires (so the SDK is never
imported) if the workload doesn't use it. When it does fire, the SDK import is
the workload's own, which it would have paid anyway; patching adds ~nothing.

This is the ``sys.meta_path``-based deferred install flagged as future work in
:func:`novafabric.capture.hooks._is_sdk_available`. Stdlib only (ADR-0024).

Safety invariants (never violated):

* **Never block the workload.** A callback that raises is swallowed — a broken
  hook must not break the workload's own import of its SDK.
* **One-shot.** Each target fires at most once; re-imports are not re-wrapped.
* **Already-imported is honored.** If the target is already in ``sys.modules``
  when registered (rare at sitecustomize time, but possible), the callback
  fires immediately so no import is missed.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec
    from types import ModuleType

PostImportCallback = Callable[["ModuleType"], None]


class _PostImportLoader:
    """Wrap a real module loader and run ``callback`` after the module executes.

    Delegates every loader responsibility to the wrapped real loader; the only
    added behavior is the post-``exec_module`` callback. The callback is
    fail-open: an exception in it never propagates into the workload's import.
    """

    def __init__(self, real_loader: object, callback: PostImportCallback) -> None:
        self._real = real_loader
        self._callback = callback

    def create_module(self, spec: "ModuleSpec") -> "ModuleType | None":
        create = getattr(self._real, "create_module", None)
        if create is not None:
            return create(spec)  # type: ignore[no-any-return]
        return None

    def exec_module(self, module: "ModuleType") -> None:
        self._real.exec_module(module)  # type: ignore[attr-defined]
        try:
            self._callback(module)
        except Exception:
            # Fail-open: a hook-install failure must never break the workload's
            # own import of its SDK. The default eager path is equally defensive.
            pass

    def __getattr__(self, item: str) -> object:
        # Forward any other loader protocol method (get_filename, is_package,
        # get_source, …) to the wrapped real loader for full compatibility.
        return getattr(self._real, item)


class _PostImportFinder:
    """A ``sys.meta_path`` finder that fires one-shot callbacks on import.

    It never *loads* anything itself: for a registered target it asks the other
    finders for the real spec, then wraps that spec's loader so the callback
    runs immediately after the module's own code executes.
    """

    def __init__(self) -> None:
        self._callbacks: dict[str, PostImportCallback] = {}

    def register(self, name: str, callback: PostImportCallback) -> None:
        self._callbacks[name] = callback

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> "ModuleSpec | None":
        callback = self._callbacks.get(fullname)
        if callback is None:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec: "ModuleSpec | None" = find(fullname, path, target)
            if spec is not None and spec.loader is not None:
                # One-shot: drop before wrapping so a re-import is not re-wrapped
                # and so the callback (which may import the SDK's submodules)
                # cannot re-enter us for the same name.
                self._callbacks.pop(fullname, None)
                spec.loader = _PostImportLoader(spec.loader, callback)  # type: ignore[assignment]
                return spec
        return None


_finder: _PostImportFinder | None = None


def register_post_import_hook(name: str, callback: PostImportCallback) -> None:
    """Run ``callback(module)`` the first time ``name`` is imported.

    If ``name`` is already imported, ``callback`` fires immediately. Otherwise a
    single shared finder is installed at the front of ``sys.meta_path`` and the
    callback fires on the workload's own import of ``name``. The target is never
    imported by this function — that is the whole point.
    """
    global _finder
    existing = sys.modules.get(name)
    if existing is not None:
        try:
            callback(existing)
        except Exception:
            pass
        return
    if _finder is None:
        _finder = _PostImportFinder()
        sys.meta_path.insert(0, _finder)
    _finder.register(name, callback)


def _reset_for_testing() -> None:
    """Remove the shared finder from ``sys.meta_path`` and clear callbacks.

    Test-only: keeps meta_path clean between tests. Not part of the public API.
    """
    global _finder
    if _finder is not None:
        try:
            sys.meta_path.remove(_finder)
        except ValueError:
            pass
        _finder = None
