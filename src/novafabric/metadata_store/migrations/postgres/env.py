"""Alembic env.py for the Postgres (production) migration track.

Reads NOVAFABRIC_METADATA_DSN from the environment and applies the project's
single DSN rule, :func:`novafabric.metadata_store.dsn.to_sqlalchemy_url` (L1):

  postgresql://user:pass@host:5432/dbname          -> postgresql+psycopg://...
  postgres://user:pass@host:5432/dbname            -> postgresql+psycopg://...
  postgresql+psycopg://user:pass@host:5432/dbname  (unchanged)
  postgresql+psycopg2://user:pass@host:5432/dbname (unchanged: an explicit
                                                    driver is a deliberate choice)
"""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from novafabric.metadata_store.dsn import to_sqlalchemy_url

config = context.config  # noqa: F841 — Alembic expects this name to be accessible

_MISSING = object()


def get_url() -> str:
    """Return the SQLAlchemy URL for NOVAFABRIC_METADATA_DSN (shared rule)."""
    dsn = os.environ.get("NOVAFABRIC_METADATA_DSN", "")
    if not dsn:
        raise RuntimeError(
            "NOVAFABRIC_METADATA_DSN is not set. "
            "Export it before running alembic commands against the Postgres track."
        )
    return to_sqlalchemy_url(dsn)


def run_migrations_online() -> None:
    engine = create_engine(get_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
