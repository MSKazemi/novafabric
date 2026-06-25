"""Alembic env.py — shared entry point for sqlite and postgres migration tracks.

The correct config file selects the version location:
  alembic upgrade head                          # SQLite (alembic.ini)
  alembic -c alembic-postgres.ini upgrade head  # Postgres

Database URL resolution order:
  1. -x db_url=<dsn>                 explicit override
  2. NOVAFABRIC_DB_PATH              for SQLite (sqlite:///...)
  3. NOVAFABRIC_POSTGRES_DSN         for Postgres
  4. Default: sqlite:///~/.novafabric/registry.db
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_db_url: str | None = context.get_x_argument(as_dictionary=True).get("db_url")
if not _db_url:
    _is_postgres = "postgres" in (config.config_file_name or "")
    if _is_postgres:
        _dsn = os.environ.get("NOVAFABRIC_POSTGRES_DSN", "")
        if not _dsn:
            raise ValueError(
                "Postgres migration selected but NOVAFABRIC_POSTGRES_DSN is not set."
            )
        _db_url = _dsn
    else:
        _sqlite_path = os.environ.get(
            "NOVAFABRIC_DB_PATH",
            str(Path.home() / ".novafabric" / "registry.db"),
        )
        _db_url = f"sqlite:///{_sqlite_path}"

config.set_main_option("sqlalchemy.url", _db_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=None,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
