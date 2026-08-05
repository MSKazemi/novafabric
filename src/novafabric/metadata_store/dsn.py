"""DSN normalisation shared between the runtime store and Alembic migrations.

Two consumers read the same Postgres DSN and disagree about what it means:

* ``metadata_store.postgres`` passes it straight to ``psycopg.connect()``, which
  wants a plain libpq URL — ``postgresql://user:pass@host:5432/db``.
* Alembic hands it to SQLAlchemy, which resolves the bare ``postgresql://``
  scheme to the **psycopg2** dialect. NovaFabric ships ``psycopg[binary]``
  (psycopg 3, Tier B under ADR-0024) and does not ship psycopg2.

So the DSN that works everywhere else made the documented migration command
fail with ``ModuleNotFoundError: No module named 'psycopg2'``. It broke CI's
integration job on every run from at least 2026-07-30 to 2026-08-05, and it
would break any operator following the migration runbook with an ordinary
connection string — which is the only kind most Postgres tooling hands you.
"""

from __future__ import annotations

__all__ = ["to_sqlalchemy_url"]

_PSYCOPG3_DIALECT = "postgresql+psycopg://"


def to_sqlalchemy_url(dsn: str) -> str:
    """Return ``dsn`` with a SQLAlchemy dialect that matches the shipped driver.

    A bare ``postgresql://`` or ``postgres://`` scheme is rewritten to
    ``postgresql+psycopg://``. Any DSN that already names a driver — including
    ``postgresql+asyncpg://`` or an explicit ``postgresql+psycopg2://`` — is
    returned unchanged, because naming a driver is an explicit choice and this
    function is not entitled to override it.

    Non-Postgres URLs (``sqlite:///…``) pass through untouched.
    """
    for scheme in ("postgresql://", "postgres://"):
        if dsn.startswith(scheme):
            return _PSYCOPG3_DIALECT + dsn[len(scheme) :]
    return dsn
