"""Masker registration: entry points + dotted import paths (ADR-0135 D2).

Two stdlib-only, Tier-A registration paths:

1. **Entry point** — a package advertises maskers under the
   ``novafabric.maskers`` entry-point group::

       [project.entry-points."novafabric.maskers"]
       acme-case-id = "novafabric_acme.masking:AcmeCaseIdMasker"

2. **Dotted import path** — ``masking.yaml`` names
   ``pkg.module:ClassName`` (or ``pkg.module.ClassName``) directly.

Registration is **fail-closed**: a masker that cannot be loaded or does not
satisfy :class:`~novafabric.masking._models.MaskerProtocol` raises
:class:`MaskerRegistrationError` at capture start — before the workload
runs — so a mis-configured pipeline can never silently skip expected
masking.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import import_module, metadata
from typing import Any

from novafabric.masking._models import MaskerProtocol, MaskerSpec

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "novafabric.maskers"


class MaskerRegistrationError(Exception):
    """A configured masker could not be loaded or is invalid."""


@dataclass(frozen=True)
class LoadedMasker:
    """A resolved masker instance paired with its config spec."""

    masker: MaskerProtocol
    spec: MaskerSpec


def _load_entry_point(name: str) -> Any | None:
    """Return the loaded entry-point target for ``name``, or None."""
    try:
        eps = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 — defensive, importlib quirks
        logger.warning("novafabric.masking: entry_points lookup failed (%s)", exc)
        return None
    for ep in eps:
        if ep.name == name:
            try:
                return ep.load()
            except Exception as exc:
                raise MaskerRegistrationError(
                    f"masker entry point {name!r} failed to load: {exc}"
                ) from exc
    return None


def _load_import_path(path: str) -> Any | None:
    """Resolve ``pkg.mod:Attr`` or ``pkg.mod.Attr``; None when not importable."""
    module_name: str
    attr: str
    if ":" in path:
        module_name, _, attr = path.partition(":")
    elif "." in path:
        module_name, _, attr = path.rpartition(".")
    else:
        return None
    if not module_name or not attr:
        return None
    try:
        module = import_module(module_name)
    except ImportError:
        return None
    try:
        return getattr(module, attr)
    except AttributeError:
        return None


def _validate_masker(obj: Any, spec: MaskerSpec) -> MaskerProtocol:
    """Check the plugin contract; raise MaskerRegistrationError otherwise."""
    masker_id = getattr(obj, "masker_id", None)
    masker_version = getattr(obj, "masker_version", None)
    pattern_ids = getattr(obj, "pattern_ids", None)
    mask = getattr(obj, "mask", None)
    problems: list[str] = []
    if not isinstance(masker_id, str) or not masker_id:
        problems.append("missing non-empty str attribute 'masker_id'")
    if not isinstance(masker_version, str) or not masker_version:
        problems.append("missing non-empty str attribute 'masker_version'")
    if not pattern_ids or not all(isinstance(p, str) for p in pattern_ids):
        problems.append("missing non-empty 'pattern_ids' sequence of str")
    if not callable(mask):
        problems.append("missing callable 'mask(field, value, context)'")
    if problems:
        raise MaskerRegistrationError(
            f"masker {spec.id!r} does not satisfy the masker contract: "
            + "; ".join(problems)
        )
    if spec.version is not None and spec.version != masker_version:
        logger.warning(
            "novafabric.masking: masker %r version mismatch: config expects %r, "
            "masker declares %r",
            spec.id, spec.version, masker_version,
        )
    return obj  # type: ignore[no-any-return]


def resolve_masker(spec: MaskerSpec) -> MaskerProtocol:
    """Resolve one masker spec to a validated masker instance.

    Resolution order: entry-point name in ``novafabric.maskers``, then
    dotted import path. A class target is instantiated with no arguments.
    """
    target = _load_entry_point(spec.id)
    if target is None:
        target = _load_import_path(spec.id)
    if target is None:
        raise MaskerRegistrationError(
            f"masker {spec.id!r} not found: no '{ENTRY_POINT_GROUP}' entry point "
            f"and not an importable dotted path"
        )
    if isinstance(target, type):
        try:
            target = target()
        except Exception as exc:
            raise MaskerRegistrationError(
                f"masker {spec.id!r} could not be instantiated: {exc}"
            ) from exc
    return _validate_masker(target, spec)


def load_maskers(specs: list[MaskerSpec]) -> list[LoadedMasker]:
    """Resolve every configured masker, preserving declared order."""
    return [LoadedMasker(masker=resolve_masker(spec), spec=spec) for spec in specs]
