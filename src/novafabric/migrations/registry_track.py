"""Programmatic access to the registry-track alembic trees (ADR-0211, experimental).

The registry track lives at the repo root (``alembic/{sqlite,postgres}/versions``)
during development and is shipped inside the wheel as
``novafabric/migrations/registry/`` (ADR-0211 D2). This module resolves
whichever copy is available and provides:

- :func:`resolve_script_dir` — packaged tree first, source checkout fallback;
- :func:`script_head` — head revision of a track backend (cached; ``None``
  when genuinely unresolvable — never a fabricated value);
- :func:`upgrade_registry_to_head` — programmatic ``alembic upgrade head``
  against an explicit database URL (the engine behind
  ``nova db upgrade --track registry`` and restore step 3).

The database URL is treated like any connection string in this codebase: it is
never logged and never embedded in error messages.
"""

from __future__ import annotations

import threading
from argparse import Namespace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from alembic.config import Config

#: Track backends and their version-tree subdirectories.
REGISTRY_TRACK_BACKENDS = ("sqlite", "postgres")


class RegistryMigrationsUnavailableError(Exception):
    """The registry migration scripts (or alembic itself) cannot be resolved."""


_HEAD_CACHE: dict[tuple[str, str], str | None] = {}
_HEAD_CACHE_LOCK = threading.Lock()


def packaged_script_dir() -> Path | None:
    """The wheel-packaged registry tree (``novafabric/migrations/registry/``)."""
    candidate = Path(__file__).resolve().parent / "registry"
    if (candidate / "env.py").is_file():
        return candidate
    return None


def checkout_script_dir() -> Path | None:
    """The repo-root ``alembic/`` tree, when running from a source checkout."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "alembic.ini").is_file() and (parent / "alembic" / "env.py").is_file():
            return parent / "alembic"
    return None


def resolve_script_dir() -> Path | None:
    """Registry-track script directory: packaged copy first, checkout fallback."""
    return packaged_script_dir() or checkout_script_dir()


def registry_alembic_config(
    backend: str,
    *,
    url: str | None = None,
    script_dir: Path | None = None,
) -> "Config":
    """Build a programmatic alembic ``Config`` for the registry *backend* track.

    Args:
        backend: ``"sqlite"`` or ``"postgres"`` (version-tree selection).
        url: Database URL; passed to ``env.py`` via the ``db_url`` x-argument
            so the environment's env-var fallbacks never apply. Never logged.
        script_dir: Explicit script directory (tests); default resolves the
            packaged tree, then the source checkout.

    Raises:
        RegistryMigrationsUnavailableError: unknown backend, alembic not
            installed, or no script tree found (lean install).
    """
    if backend not in REGISTRY_TRACK_BACKENDS:
        raise RegistryMigrationsUnavailableError(
            f"Unknown registry track backend {backend!r} — "
            f"expected one of {REGISTRY_TRACK_BACKENDS}"
        )
    try:
        from alembic.config import Config  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - alembic is a runtime dep
        raise RegistryMigrationsUnavailableError(
            "alembic is not installed — registry-track migrations unavailable"
        ) from exc

    resolved = script_dir or resolve_script_dir()
    if resolved is None:
        raise RegistryMigrationsUnavailableError(
            "Registry migration scripts not found (neither the packaged "
            "novafabric/migrations/registry tree nor a source checkout)"
        )
    cfg = Config()
    cfg.set_main_option("script_location", str(resolved))
    cfg.set_main_option("version_locations", str(resolved / backend / "versions"))
    cfg.set_main_option("path_separator", "os")  # alembic >=1.16 deprecation
    if url is not None:
        # env.py resolves `-x db_url=…` first — explicit beats ambient env vars.
        cfg.cmd_opts = Namespace(x=[f"db_url={_sqlalchemy_url(url)}"])
    return cfg


def _sqlalchemy_url(url: str) -> str:
    """Normalize a libpq DSN to a psycopg3 SQLAlchemy URL.

    Bare ``postgresql://`` URLs make SQLAlchemy pick the psycopg2 driver,
    which is not a NovaFabric dependency — pin the installed psycopg3 driver.
    SQLite URLs and already-driver-qualified URLs pass through unchanged.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def script_head(backend: str, *, script_dir: Path | None = None) -> str | None:
    """Head revision of the registry *backend* track, or ``None`` if unresolvable.

    The result is cached per (backend, script dir) — script trees are immutable
    for the lifetime of a process. Never raises: an unresolvable head is an
    honest ``None`` (the caller must treat it as ``unknown``, never ``ok``).
    """
    resolved = script_dir or resolve_script_dir()
    cache_key = (backend, str(resolved) if resolved is not None else "")
    with _HEAD_CACHE_LOCK:
        if cache_key in _HEAD_CACHE:
            return _HEAD_CACHE[cache_key]
    head: str | None = None
    try:
        from alembic.script import ScriptDirectory  # noqa: PLC0415

        cfg = registry_alembic_config(backend, script_dir=resolved)
        head = ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:  # noqa: BLE001 — alembic absent / no scripts / multi-head
        head = None
    with _HEAD_CACHE_LOCK:
        _HEAD_CACHE[cache_key] = head
    return head


def head_ancestry(backend: str, *, script_dir: Path | None = None) -> set[str] | None:
    """Revisions on the path base→head for the track, or ``None`` if unresolvable."""
    try:
        from alembic.script import ScriptDirectory  # noqa: PLC0415

        cfg = registry_alembic_config(backend, script_dir=script_dir)
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
        if head is None:
            return None
        return {rev.revision for rev in script.iterate_revisions(head, "base")}
    except Exception:  # noqa: BLE001 — same honesty rule as script_head
        return None


def clear_head_cache() -> None:
    """Reset the head cache (tests)."""
    with _HEAD_CACHE_LOCK:
        _HEAD_CACHE.clear()


def upgrade_registry_to_head(
    backend: str,
    url: str,
    *,
    script_dir: Path | None = None,
) -> str | None:
    """Run ``alembic upgrade head`` for the registry *backend* track against *url*.

    Returns the script head revision after the upgrade (best-effort; ``None``
    when it cannot be re-resolved). Raises
    :class:`RegistryMigrationsUnavailableError` when the track cannot be
    resolved; alembic/database errors propagate to the caller, with the URL
    never echoed by this function.
    """
    from alembic import command  # noqa: PLC0415

    cfg = registry_alembic_config(backend, url=url, script_dir=script_dir)
    command.upgrade(cfg, "head")
    return script_head(backend, script_dir=script_dir)
