"""Alembic env.py for the Postgres (production) migration track.

Reads NOVAFABRIC_METADATA_DSN from the environment and normalises the
driver prefix to ``postgresql+psycopg`` (psycopg3).

Example DSNs accepted:
  postgresql://user:pass@host:5432/dbname
  postgresql+psycopg://user:pass@host:5432/dbname
  postgresql+psycopg2://user:pass@host:5432/dbname  (normalised to psycopg3)
"""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

config = context.config  # noqa: F841 — Alembic expects this name to be accessible

_MISSING = object()


def get_url() -> str:
    """Return a SQLAlchemy URL with the psycopg3 driver prefix."""
    dsn = os.environ.get("NOVAFABRIC_METADATA_DSN", "")
    if not dsn:
        raise RuntimeError(
            "NOVAFABRIC_METADATA_DSN is not set. "
            "Export it before running alembic commands against the Postgres track."
        )
    # Normalise driver prefix to psycopg3.
    dsn = dsn.replace("postgresql+psycopg2://", "postgresql://")
    if not dsn.startswith("postgresql+psycopg://"):
        dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return dsn


def run_migrations_online() -> None:
    engine = create_engine(get_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
