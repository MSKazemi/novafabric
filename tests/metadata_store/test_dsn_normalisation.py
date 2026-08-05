"""The Postgres DSN must name a driver this project actually ships.

Regression guard for a defect that broke CI's `integration` job on every run
from at least 2026-07-30 to 2026-08-05, and that would break any operator
following the documented migration command:

    alembic -c alembic-postgres.ini upgrade head
    ModuleNotFoundError: No module named 'psycopg2'

SQLAlchemy resolves the bare ``postgresql://`` scheme to psycopg2. NovaFabric
depends on ``psycopg[binary]`` (psycopg 3) and does not ship psycopg2 — so the
same DSN that works for ``psycopg.connect()`` everywhere else in the codebase
failed the moment SQLAlchemy touched it.
"""

from __future__ import annotations

import pytest

from novafabric.metadata_store.dsn import to_sqlalchemy_url


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "postgresql://postgres:test@localhost:5432/novafabric_test",
            "postgresql+psycopg://postgres:test@localhost:5432/novafabric_test",
        ),
        # The short scheme libpq and many hosting providers emit.
        (
            "postgres://user:pw@db.example.com/nova",
            "postgresql+psycopg://user:pw@db.example.com/nova",
        ),
    ],
)
def test_bare_postgres_schemes_get_the_psycopg3_dialect(dsn: str, expected: str) -> None:
    assert to_sqlalchemy_url(dsn) == expected


@pytest.mark.parametrize(
    "dsn",
    [
        # Already correct — must not be rewritten twice.
        "postgresql+psycopg://user:pw@host/db",
        # An explicit driver is a deliberate choice; do not override it, even
        # when it is the driver that caused the original bug.
        "postgresql+psycopg2://user:pw@host/db",
        "postgresql+asyncpg://user:pw@host/db",
        # Not Postgres at all.
        "sqlite:///tmp/registry.db",
    ],
)
def test_an_explicit_driver_is_left_alone(dsn: str) -> None:
    assert to_sqlalchemy_url(dsn) == dsn


def test_normalisation_is_idempotent() -> None:
    once = to_sqlalchemy_url("postgresql://u:p@h/d")
    assert to_sqlalchemy_url(once) == once


def test_credentials_and_query_parameters_survive() -> None:
    dsn = "postgresql://u:p%40ss@h:5432/d?sslmode=require&application_name=nova"

    result = to_sqlalchemy_url(dsn)

    assert result == (
        "postgresql+psycopg://u:p%40ss@h:5432/d?sslmode=require&application_name=nova"
    )
