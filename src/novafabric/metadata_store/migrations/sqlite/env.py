"""Alembic env.py for the SQLite (dev-only) migration track.

Reads NOVAFABRIC_DB_PATH from the environment; defaults to
~/.novafabric/metadata.db when unset.
"""
from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine

config = context.config  # noqa: F841 — Alembic expects this name to be accessible


def get_url() -> str:
    db_path = os.environ.get(
        "NOVAFABRIC_DB_PATH", str(Path.home() / ".novafabric" / "metadata.db")
    )
    return f"sqlite:///{db_path}"


def run_migrations_online() -> None:
    engine = create_engine(get_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
