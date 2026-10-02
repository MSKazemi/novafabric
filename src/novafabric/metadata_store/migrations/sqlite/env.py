"""Alembic env.py for the SQLite (dev-only) migration track.

Reads NOVAFABRIC_DB_PATH from the environment; defaults to
$NOVAFABRIC_HOME/metadata.db (default ~/.novafabric) when unset.
"""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from novafabric import _paths as _nf_paths

config = context.config  # noqa: F841 — Alembic expects this name to be accessible


def get_url() -> str:
    db_path = os.environ.get(
        "NOVAFABRIC_DB_PATH", str(_nf_paths.nova_home() / "metadata.db")
    )
    # A fresh custom NOVAFABRIC_HOME does not exist yet and sqlite will not
    # create the parent directory, so create it before connecting.
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    return f"sqlite:///{db_path}"


def run_migrations_online() -> None:
    engine = create_engine(get_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
