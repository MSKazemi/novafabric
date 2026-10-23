"""Where the signed promotion policy lives by default: the resolved NovaSeal log.

The maker-checker ``PolicyStore`` shares its SQLite file with the NovaSeal Merkle
log. Until 2026-10-23 the CLI (``nova policy sign``, ``nova seal propose/approve/
verify/bypass``, ``nova policy list``, ``nova export-model-independence``) and
``POST /api/policy/sign`` defaulted to ``~/.local/share/novafabric/merkle.db``,
while the dashboard SealTab and server mode read the policy from the resolved
Merkle log. A policy signed on one side was invisible to the other.

Every default now goes through :func:`default_policy_db`, which is the single
NovaSeal log resolver (``novafabric.trust.novaseal.config.resolve_merkle_db_uri``):

1. ``NOVAFABRIC_SEAL_DB_PATH``;
2. ``merkle_db`` in the discovered ``novaseal.yaml``;
3. ``$NOVAFABRIC_HOME/novaseal-merkle.db``.

An explicit ``--db`` (or ``db_path`` in the request) still wins. ``PolicyStore`` is
SQLite-only, so a Postgres log location is refused with a hint to pass ``--db``.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "PolicyDbLocationError",
    "default_policy_db",
    "legacy_policy_db",
    "legacy_policy_db_path",
]


class PolicyDbLocationError(ValueError):
    """The default policy store cannot be derived (a Postgres log, a bad ``merkle_db``)."""


def legacy_policy_db_path() -> Path:
    """The pre-2026-10-23 CLI default, ``~/.local/share/novafabric/merkle.db``."""
    return Path.home() / ".local" / "share" / "novafabric" / "merkle.db"


def default_policy_db() -> Path:
    """The SQLite file the promotion policy is read from and written to by default.

    Raises:
        PolicyDbLocationError: the resolved log is a Postgres DSN (shown with its
            password redacted) or ``merkle_db`` is neither a path nor a DSN.
    """
    from novafabric.trust.novaseal.config import (  # noqa: PLC0415
        SealConfigError,
        resolve_merkle_db_uri,
    )
    from novafabric.trust.novaseal.log_location import (  # noqa: PLC0415
        is_postgres_dsn,
        redact_location,
    )

    try:
        location = resolve_merkle_db_uri()
    except SealConfigError as exc:
        raise PolicyDbLocationError(f"cannot locate the promotion-policy store: {exc}") from exc
    if is_postgres_dsn(location):
        raise PolicyDbLocationError(
            f"the NovaSeal Merkle log is a Postgres DSN ({redact_location(location)}); "
            "the promotion-policy store is a SQLite file — pass --db <path>"
        )
    return Path(location)


def legacy_policy_db() -> Path | None:
    """The legacy store when it exists and is not the current default, else None.

    Lets a command that finds no policy name the old file a policy may still be in.
    """
    legacy = legacy_policy_db_path()
    if not legacy.is_file():
        return None
    try:
        current = default_policy_db()
    except PolicyDbLocationError:
        return legacy
    return None if current.expanduser() == legacy else legacy
