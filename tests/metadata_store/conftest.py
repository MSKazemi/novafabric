"""Shared fixtures for metadata_store integration tests.

The ``postgres_url`` session fixture yields a Postgres 16 DSN from one of two
sources, in order:

1. ``NOVA_TEST_POSTGRES_DSN`` — a Postgres you are already running. testcontainers
   4.15 has no container-reuse API, so a fresh container is started per session
   and the tier costs minutes on every run. Pointing this at a long-lived local
   Postgres makes the tier warm::

       docker run -d --name nova-test-pg -p 5433:5432 \
           -e POSTGRES_PASSWORD=postgres postgres:16-alpine
       export NOVA_TEST_POSTGRES_DSN=postgresql://postgres:postgres@localhost:5433/postgres

   The variable is ``NOVA_``-prefixed on purpose: the hermetic fixture in
   ``tests/conftest.py`` strips every ``NOVAFABRIC_*`` var from the environment,
   which would delete a ``NOVAFABRIC_``-prefixed name before it could be read.

2. testcontainers — the default, and what CI always uses.

If neither is available the fixture skips gracefully, and every test that
depends on it is skipped with a clear message.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlparse, urlunparse

import pytest

#: A DSN supplied here is used *instead of* starting a container.
_DSN_ENV = "NOVA_TEST_POSTGRES_DSN"
#: This tier creates and drops schemas, runs migrations and truncates tables. A
#: typo that points it at a shared or production database would be destructive
#: and silent, so a non-local host has to be affirmed deliberately.
_ALLOW_REMOTE_ENV = "NOVA_TEST_POSTGRES_ALLOW_REMOTE"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


def _admin_dsn_from_env() -> str | None:
    """Return the developer-supplied DSN, refusing a remote host by default."""
    dsn = os.environ.get(_DSN_ENV)
    if not dsn:
        return None
    host = urlparse(dsn).hostname or ""
    if host not in _LOCAL_HOSTS and os.environ.get(_ALLOW_REMOTE_ENV) != "1":
        pytest.fail(
            f"{_DSN_ENV} points at a non-local host ({host!r}). This tier "
            "creates and drops databases, runs migrations and truncates tables "
            "— it will destroy data there. Set "
            f"{_ALLOW_REMOTE_ENV}=1 only if that server is genuinely disposable."
        )
    return dsn


@contextmanager
def _fresh_database(admin_dsn: str) -> Iterator[str]:
    """Create a throwaway database on *admin_dsn*'s server; drop it after.

    A supplied DSN is a *server*, not a session. Handing tests the same database
    twice is not equivalent to a fresh container: measured here, reusing one
    database made ``test_postgres_bootstrap_idempotent`` fail on the second run
    while passing in isolation, because this tier bootstraps schemas and expects
    to be the only thing that ever has. Creating one database per session keeps
    the warm server (the expensive part) and the clean slate (the correct part).
    """
    import psycopg
    from psycopg import sql

    name = f"nova_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        parts = urlparse(admin_dsn)
        yield urlunparse(parts._replace(path=f"/{name}"))
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            # FORCE (PG13+) evicts any connection a test leaked, so teardown
            # cannot hang on a stray session.
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Pin every metadata_store test to one xdist worker (suite-health 2026-07-15).

    The ``postgres_url`` fixture is session-scoped, but under pytest-xdist each
    worker has its own session — ``-n 12`` would start up to 12 simultaneous
    Postgres containers and container-start contention makes the tier flaky.
    With ``--dist=loadgroup`` (the documented gate invocation) this single group
    runs on one worker and shares one container; under plain ``--dist=load`` the
    marker is inert.
    """
    for item in items:
        if "tests/metadata_store" in str(item.path):
            item.add_marker(pytest.mark.xdist_group("metadata-store-postgres"))


@pytest.fixture(scope="session")
def postgres_url():
    """Return a psycopg3-compatible Postgres DSN for this session.

    Prefers ``NOVA_TEST_POSTGRES_DSN`` when set (see the module docstring);
    otherwise starts an ephemeral container. Skips the session if Docker is
    unavailable or testcontainers is not installed. The URL uses a plain
    ``postgresql://`` scheme, not the SQLAlchemy ``+psycopg2`` prefix.
    """
    admin_dsn = _admin_dsn_from_env()
    if admin_dsn is not None:
        with _fresh_database(admin_dsn) as dsn:
            yield dsn
        return

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
