"""Migration `v004` — widening the `runs` key, and the downgrade back (RFC-0001).

`v004` rebuilds a partitioned table and copies every row. That is the riskiest
kind of migration to get wrong, and its `downgrade()` is *lossy by construction*:
the narrower `(run_id, started_at)` key cannot hold two tenants that share a
`run_id` at the same timestamp. Both directions are exercised here against a real
Postgres, because a migration verified only by reading it is not verified.

Follows `test_partition_ddl.py`: a **function-scoped** container per test, so DDL
that rewrites `runs` cannot pollute the shared session container other modules
use.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.xdist_group("metadata-store-postgres")


@pytest.fixture(scope="function")
def postgres_url():
    """A fresh, isolated Postgres per test — this module rewrites `runs`."""
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed")

    try:
        with PostgresContainer("postgres:16-alpine") as container:
            url: str = container.get_connection_url()
            yield url.replace("postgresql+psycopg2://", "postgresql://").replace(
                "postgresql+psycopg://", "postgresql://"
            )
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Could not start Postgres container: {exc}")


class _Op:
    """Minimal stand-in for Alembic's `op`, so the migration runs unmodified.

    The revision only ever calls `op.execute`, so binding that to psycopg lets
    the real migration code be tested rather than a paraphrase of it.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str) -> None:
        self._conn.execute(sql)


def _load_v004():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "src/novafabric/metadata_store/migrations/postgres/versions"
        / "v004_runs_tenant_in_pk.py"
    )
    spec = importlib.util.spec_from_file_location("_v004", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_v002_shape(conn: Any) -> None:
    """Create `runs` as v002 leaves it: partitioned, keyed `(run_id, started_at)`."""
    conn.execute("CREATE ROLE novafabric_app")
    conn.execute("CREATE ROLE novafabric_migrator")
    conn.execute(
        """
        CREATE TABLE runs (
            run_id            UUID        NOT NULL,
            tenant_id         UUID        NOT NULL,
            event_type        TEXT,
            global_run_id     UUID,
            started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status            TEXT        NOT NULL DEFAULT 'pending',
            world_size        BIGINT,
            expected_children BIGINT,
            children_arrived  BIGINT      NOT NULL DEFAULT 0,
            CONSTRAINT runs_pkey PRIMARY KEY (run_id, started_at)
        ) PARTITION BY RANGE (started_at)
        """
    )
    conn.execute(
        "CREATE TABLE runs_y2026q1 PARTITION OF runs "
        "FOR VALUES FROM ('2026-01-01') TO ('2026-04-01')"
    )


def _pkey_columns(conn: Any) -> list[str]:
    row = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'runs'::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """
    ).fetchall()
    return [r[0] for r in row]


def test_v004_upgrade_widens_the_key_and_preserves_rows(postgres_url: str) -> None:
    v004 = _load_v004()
    run_id, tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    with psycopg.connect(postgres_url, autocommit=True) as conn:
        _seed_v002_shape(conn)
        # Distinct timestamps from the start: under v002's (run_id, started_at)
        # key these two rows would otherwise collide before the migration runs,
        # which is itself the problem RFC-0001 fixed.
        for tenant, when in ((tenant_a, "2026-02-01"), (tenant_b, "2026-02-02")):
            conn.execute(
                "INSERT INTO runs (run_id, tenant_id, started_at) VALUES (%s, %s, %s)",
                (str(run_id), str(tenant), when),
            )

        v004._swap(_Op(conn), pkey_columns="run_id, tenant_id, started_at", dedupe_on="run_id, tenant_id")

        assert _pkey_columns(conn) == ["run_id", "tenant_id", "started_at"]
        kept = conn.execute("SELECT count(*) FROM runs WHERE run_id = %s", (str(run_id),)).fetchone()
        assert kept[0] == 2, "both tenants' rows must survive the rebuild"


def test_v004_adds_a_default_partition(postgres_url: str) -> None:
    """Without it, a timestamp outside the declared quarters is rejected."""
    v004 = _load_v004()

    with psycopg.connect(postgres_url, autocommit=True) as conn:
        _seed_v002_shape(conn)
        v004._swap(_Op(conn), pkey_columns="run_id, tenant_id, started_at", dedupe_on="run_id, tenant_id")

        conn.execute(
            "INSERT INTO runs (run_id, tenant_id, started_at) VALUES (%s, %s, %s)",
            (str(uuid.uuid4()), str(uuid.uuid4()), "2019-05-01"),
        )
        out = conn.execute("SELECT count(*) FROM runs WHERE started_at < '2024-01-01'").fetchone()
        assert out[0] == 1


def test_v004_downgrade_returns_to_the_narrow_key(postgres_url: str) -> None:
    """The reverse direction, including its documented lossiness.

    Two tenants sharing a `run_id` *and* a `started_at` cannot both fit the
    narrower key; `DISTINCT ON` keeps one rather than aborting. Asserting that
    explicitly is the point — a downgrade whose data loss is a surprise is worse
    than one that is documented.
    """
    v004 = _load_v004()
    run_id = uuid.uuid4()

    with psycopg.connect(postgres_url, autocommit=True) as conn:
        _seed_v002_shape(conn)
        v004._swap(_Op(conn), pkey_columns="run_id, tenant_id, started_at", dedupe_on="run_id, tenant_id")

        for tenant in (uuid.uuid4(), uuid.uuid4()):
            conn.execute(
                "INSERT INTO runs (run_id, tenant_id, started_at) VALUES (%s, %s, %s)",
                (str(run_id), str(tenant), "2026-02-01"),
            )
        assert conn.execute(
            "SELECT count(*) FROM runs WHERE run_id = %s", (str(run_id),)
        ).fetchone()[0] == 2

        v004._swap(_Op(conn), pkey_columns="run_id, started_at", dedupe_on="run_id, started_at")

        assert _pkey_columns(conn) == ["run_id", "started_at"]
        survivors = conn.execute(
            "SELECT count(*) FROM runs WHERE run_id = %s", (str(run_id),)
        ).fetchone()[0]
        assert survivors == 1, "the narrower key can only hold one of the two tenants"
