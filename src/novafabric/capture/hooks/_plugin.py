"""Third-party capture-hook plugin contract (RFC-0001 Option C, v0.5.x C-2).

Status: **EXPERIMENTAL in v0.5.x.** The :class:`HookProtocol` surface is
subject to change between minor versions. Stability decision is queued for
v0.7 — see the v0.5.x C-2 plan for the criteria.

Third parties extend NovaFabric's capture coverage by publishing Python
packages that expose a class implementing :class:`HookProtocol` under the
``novafabric.hooks`` entry-point group:

.. code-block:: toml

    # in some-third-party-package/pyproject.toml
    [project.entry-points."novafabric.hooks"]
    acme_sdk = "novafabric_acme.hook:AcmeHook"

NovaFabric discovers these at capture time and installs each one alongside
the built-in hooks (OpenAI / Anthropic / MCP / requests / httpx). A buggy
plugin must never break the built-in capture path — every plugin is
isolated by error handling in :func:`install_discovered_plugins`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import metadata
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "novafabric.hooks"


@dataclass(frozen=True)
class HookPluginInfo:
    """Metadata describing a plugin hook for logs and diagnostics."""

    name: str
    version: str = "unknown"
    capabilities: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class HookProtocol(Protocol):
    """The contract every hook (built-in or plugin) must satisfy.

    The Protocol covers the **minimum viable** surface. The optional
    ``info()`` accessor is not part of the runtime check, so legacy
    classes without it still pass ``isinstance(x, HookProtocol)``.
    """

    def __init__(
        self, writer: "CapsuleWriter", parent_span_id: str
    ) -> None: ...

    def install(self) -> None:
        """Patch the target SDK or library.

        Must not raise. Failures should be caught internally and leave the
        runtime in a clean state (i.e. no half-installed monkey-patches).
        """
        ...

    def uninstall(self) -> None:
        """Reverse :meth:`install`. Must be idempotent and safe to call
        even when :meth:`install` was never called or failed."""
        ...


@dataclass
class DiscoveredHook:
    """One hook discovered through the entry-point group.

    Holds the class (not yet instantiated) and any metadata available
    from the entry-point distribution. Instantiation happens later, in
    the install loop, so that error isolation can scope to each plugin.
    """

    info: HookPluginInfo
    cls: type
    entry_point_value: str


def discover_plugin_hooks() -> list[DiscoveredHook]:
    """Walk the ``novafabric.hooks`` entry-point group.

    Returns one :class:`DiscoveredHook` per loadable entry point that
    resolves to a class implementing :class:`HookProtocol`. Entry points
    that fail to import or whose target is not a class are logged at
    WARNING level and skipped.
    """
    discovered: list[DiscoveredHook] = []
    try:
        eps = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 — defensive, importlib quirks
        logger.warning("novafabric: entry_points lookup failed (%s)", exc)
        return discovered

    for ep in eps:
        try:
            target = ep.load()
        except Exception as exc:  # noqa: BLE001 — buggy plugin must not crash capture
            logger.warning(
                "novafabric: plugin '%s' failed to load: %s",
                ep.name, exc,
            )
            continue

        if not isinstance(target, type):
            logger.warning(
                "novafabric: plugin '%s' did not resolve to a class (got %r); skipping",
                ep.name, type(target).__name__,
            )
            continue

        info = _info_for(ep, target)
        if not _looks_like_hook(target):
            logger.warning(
                "novafabric: plugin '%s' (%s) does not satisfy HookProtocol; skipping",
                ep.name, info.name,
            )
            continue

        discovered.append(
            DiscoveredHook(info=info, cls=target, entry_point_value=ep.value)
        )
    return discovered


def install_discovered_plugins(
    writer: "CapsuleWriter",
    parent_span_id: str,
    *,
    discovered: list[DiscoveredHook] | None = None,
) -> list[object]:
    """Instantiate and install each discovered plugin, isolating failures.

    Returns the list of successfully installed plugin instances so the
    caller can later call ``uninstall()`` on each. Failures are logged
    and skipped — the returned list contains only plugins whose
    constructor and ``install()`` both completed without raising.
    """
    plugins = discovered if discovered is not None else discover_plugin_hooks()
    installed: list[object] = []
    for hook in plugins:
        try:
            instance = hook.cls(writer=writer, parent_span_id=parent_span_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "novafabric: plugin '%s' constructor raised (%s); skipping",
                hook.info.name, exc,
            )
            continue
        try:
            instance.install()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "novafabric: plugin '%s' install() raised (%s); skipping",
                hook.info.name, exc,
            )
            continue
        installed.append(instance)
    return installed


def _info_for(ep: metadata.EntryPoint, target: type) -> HookPluginInfo:
    """Best-effort metadata: prefer the target's own ``info()``, fall back
    to entry-point dist info, fall back to entry-point name."""
    info_method = getattr(target, "info", None)
    if callable(info_method):
        try:
            # Works for @classmethod (bound) and @staticmethod. Plain
            # instance methods will fail (no instance yet) and we fall
            # back — that is the documented contract.
            candidate = info_method()
            if isinstance(candidate, HookPluginInfo):
                return candidate
        except Exception:  # noqa: BLE001 — info() is optional and best-effort
            pass

    version = "unknown"
    dist = getattr(ep, "dist", None)
    if dist is not None:
        version = getattr(dist, "version", None) or "unknown"
    return HookPluginInfo(name=ep.name, version=version)


def _looks_like_hook(target: type) -> bool:
    """`isinstance` check is unreliable on uninstantiated classes; instead
    inspect the class for the two required methods."""
    return callable(getattr(target, "install", None)) and callable(
        getattr(target, "uninstall", None)
    )
