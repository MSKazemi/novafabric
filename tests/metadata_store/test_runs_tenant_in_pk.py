"""RFC-0001 option C: `tenant_id` is part of the `runs` primary key.

Three defects sat on top of each other here, and each was hidden by the one
above it (issue #23):

1. v002 partitioned `runs` by `started_at`. Postgres requires the partition
   column in every unique constraint, so the key became `(run_id, started_at)`
   and `tenant_id` fell out. `register_run`'s `ON CONFLICT (run_id, tenant_id)`
   could no longer be inferred → **every insert raised**.
2. Behind that: `started_at` is `NOT NULL` on the partitioned table so it can
   route to a child, but `register_run` passed an explicit `NULL` when the
   caller omitted it → *"no partition of relation found for row"*.
3. Behind that: the declared partitions covered only 2024–2027, with no
   `DEFAULT`, so any timestamp outside that window was rejected — a cliff the
   calendar reaches on its own.

These tests pin the behaviour rather than the implementation, so a future change
to the partition strategy has to keep the contract or fail loudly.
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest

from novafabric.metadata_store.postgres import PostgresMetadataStore

pytestmark = pytest.mark.xdist_group("metadata-store-postgres")


@pytest.fixture()
def store(postgres_url: str) -> PostgresMetadataStore:
    s = PostgresMetadataStore(dsn=postgres_url)
    s.bootstrap()
    return s


def _count(dsn: str, **where: str) -> int:
    clause = " AND ".join(f"{k} = %s" for k in where)
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(f"SELECT count(*) FROM runs WHERE {clause}", tuple(where.values()))  # noqa: S608
        return int(row.fetchone()[0])


def test_register_run_without_started_at_succeeds(
    store: PostgresMetadataStore, postgres_url: str
) -> None:
    """Defect 2: an omitted `started_at` must not reach the partition key as NULL."""
    run_id, tenant_id = uuid.uuid4(), uuid.uuid4()

    with store.begin_tenant_context(tenant_id) as scoped:
        scoped.register_run(run_id, tenant_id)

    assert _count(postgres_url, run_id=str(run_id), tenant_id=str(tenant_id)) == 1


def test_registering_the_same_run_twice_is_idempotent(
    store: PostgresMetadataStore, postgres_url: str
) -> None:
    """The documented contract: idempotent on `(run_id, tenant_id)`."""
    run_id, tenant_id = uuid.uuid4(), uuid.uuid4()

    with store.begin_tenant_context(tenant_id) as scoped:
        scoped.register_run(run_id, tenant_id)
        scoped.register_run(run_id, tenant_id)

    assert _count(postgres_url, run_id=str(run_id), tenant_id=str(tenant_id)) == 1


def test_idempotency_holds_even_when_started_at_differs(
    store: PostgresMetadataStore, postgres_url: str
) -> None:
    """The case a three-column key alone would NOT catch.

    With `PRIMARY KEY (run_id, tenant_id, started_at)`, a re-registration under a
    different timestamp is a distinct key and the database would happily insert
    a second row. `register_run`'s `WHERE NOT EXISTS` guard is what keeps the
    documented contract, so this test is the one that fails if that guard is
    ever swapped back for a plain `ON CONFLICT`.
    """
    run_id, tenant_id = uuid.uuid4(), uuid.uuid4()

    with store.begin_tenant_context(tenant_id) as scoped:
        scoped.register_run(run_id, tenant_id, started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
        scoped.register_run(run_id, tenant_id, started_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC))

    assert _count(postgres_url, run_id=str(run_id), tenant_id=str(tenant_id)) == 1


def test_the_same_run_id_under_two_tenants_is_two_rows(
    store: PostgresMetadataStore, postgres_url: str
) -> None:
    """Why `tenant_id` belongs in the key at all.

    Two tenants may legitimately use the same `run_id`. Under v002's
    `(run_id, started_at)` key they could collide; RFC-0001 makes the separation
    structural rather than incidental.
    """
    run_id = uuid.uuid4()
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    for tenant in (tenant_a, tenant_b):
        with store.begin_tenant_context(tenant) as scoped:
            scoped.register_run(run_id, tenant)

    assert _count(postgres_url, run_id=str(run_id)) == 2
    assert _count(postgres_url, run_id=str(run_id), tenant_id=str(tenant_a)) == 1


@pytest.mark.parametrize(
    "started_at",
    [
        pytest.param(dt.datetime(2019, 5, 1, tzinfo=dt.UTC), id="before-declared-quarters"),
        pytest.param(dt.datetime(2031, 5, 1, tzinfo=dt.UTC), id="after-declared-quarters"),
    ],
)
def test_a_timestamp_outside_the_declared_quarters_is_stored(
    store: PostgresMetadataStore, postgres_url: str, started_at: dt.datetime
) -> None:
    """Defect 3: the DEFAULT partition, without which the calendar breaks this."""
    run_id, tenant_id = uuid.uuid4(), uuid.uuid4()

    with store.begin_tenant_context(tenant_id) as scoped:
        scoped.register_run(run_id, tenant_id, started_at=started_at)

    assert _count(postgres_url, run_id=str(run_id), tenant_id=str(tenant_id)) == 1
