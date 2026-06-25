"""Shared fixtures for metadata_store integration tests.

The ``postgres_url`` session fixture spins up a Postgres 16-alpine container
via testcontainers.  If Docker is unavailable or testcontainers cannot be
imported the fixture skips gracefully, and all tests that depend on it
are skipped with a clear message.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def postgres_url():
    """Return a psycopg3-compatible connection URL to an ephemeral Postgres container.

    Skips the test session if Docker is unavailable or testcontainers is not installed.
    The URL uses the ``psycopg`` driver prefix (psycopg3) rather than ``psycopg2``.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed — skipping Postgres integration tests")

    try:
        with PostgresContainer("postgres:16-alpine") as container:
            url: str = container.get_connection_url()
            # testcontainers returns a psycopg2 URL; normalise to psycopg3 driver prefix
            url = url.replace("postgresql+psycopg2://", "postgresql+psycopg://")
            # Strip SQLAlchemy driver prefix — psycopg.connect() expects a plain DSN
            # or a libpq-style connection string.  We derive the raw DSN from the URL.
            raw_dsn = url.replace("postgresql+psycopg://", "postgresql://")
            yield raw_dsn
    except Exception as exc:
        pytest.skip(f"Could not start Postgres container (Docker unavailable?): {exc}")
