"""Where a NovaSeal Merkle log lives: a SQLite path or a Postgres DSN.

A log location is a string. It is never passed through ``Path()`` before it is
known to be a path: ``Path("postgres://u:p@h/db")`` collapses ``//`` to ``/``,
and the result looks like a relative file path. Until audit H2 (2026-10-22) the
config loader then created that path as a directory tree in the current
directory — password included — and capture sealing opened a SQLite file there
instead of the configured Postgres log.

Accepted DSN schemes: ``postgres://``, ``postgresql://`` and the SQLAlchemy
spelling ``postgresql+psycopg://`` (rewritten to ``postgresql://``, the form
psycopg 3 connects with). Any other ``scheme://`` — ``postgresql+psycopg2://``
(a driver NovaFabric does not ship), ``mysql://``, … — and a DSN that already
lost a slash (``postgres:/…``) are refused, never opened as a file.

A DSN is shown to people only through :func:`redact_location`, which drops the
password; nothing in NovaSeal writes a DSN to disk.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "MerkleLocationError",
    "is_postgres_dsn",
    "normalize_location",
    "redact_location",
]

_LIBPQ_SCHEMES = ("postgres://", "postgresql://")
_SQLALCHEMY_PSYCOPG = "postgresql+psycopg://"
_URL_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+:/")
_USERINFO_PASSWORD = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]+:/+[^/:@]*):[^@]*@")


class MerkleLocationError(ValueError):
    """A configured Merkle log location is neither a file path nor a usable DSN."""


def is_postgres_dsn(location: str | Path | None) -> bool:
    """True for a Postgres DSN NovaSeal opens with psycopg (after normalisation)."""
    if location is None or isinstance(location, Path):
        return False
    return location.startswith((*_LIBPQ_SCHEMES, _SQLALCHEMY_PSYCOPG))


def redact_location(location: str | Path) -> str:
    """*location* safe to print: a URL's password is replaced with ``***``.

    Works on malformed URLs too (``postgres:/user:pw@host`` has no netloc, so a
    URL parser would not find its password).
    """
    return _USERINFO_PASSWORD.sub(r"\1:***@", str(location))


def normalize_location(location: str | Path) -> str | Path:
    """Return a DSN as a libpq ``str``, a file location as an expanded ``Path``.

    Raises :class:`MerkleLocationError` (the message never carries a password)
    for an empty value, a non-Postgres or unsupported-driver URL, or a DSN that
    lost a ``/`` (``postgres:/…``).
    """
    if isinstance(location, Path):
        return location.expanduser()
    text = location.strip()
    if not text:
        raise MerkleLocationError("merkle_db is empty")
    if text.startswith(_LIBPQ_SCHEMES):
        return text
    if text.startswith(_SQLALCHEMY_PSYCOPG):
        return "postgresql://" + text[len(_SQLALCHEMY_PSYCOPG) :]
    if _URL_LIKE.match(text):
        scheme = text.split(":", 1)[0]
        raise MerkleLocationError(
            f"merkle_db {redact_location(text)!r} is not a supported log location: "
            f"scheme {scheme!r} is not one of postgres://, postgresql://, "
            "postgresql+psycopg:// and it is not a file path"
        )
    return Path(text).expanduser()
