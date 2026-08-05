# RFC-0001 — `runs` partition key vs. tenant idempotency

**Status:** Draft
**Authors:** @MSKazemi (filed from the [#23](https://github.com/novafabric/novafabric/issues/23) investigation)
**Reviewers:** _unassigned — needs 1 maintainer + 1 community sponsor_
**Comment window:** not started (4 weeks when opened — this is spec-breaking)
**Related:** ADR-0051 (partition strategy), ADR-0016 (storage backend), issues #23, #21

## Summary

`PostgresMetadataStore.register_run()` writes `ON CONFLICT (run_id, tenant_id)`,
but after the packaged Postgres migrations reach head the `runs` table's only
unique constraint is `PRIMARY KEY (run_id, started_at)`. The two cannot match, so
**server mode cannot index a run once it has applied its own migrations.** This
RFC asks which of the two contracts is the real one, and what replaces the other.

## Motivation

**This is not theoretical and not new.** On a clean Postgres 16, with nothing but
the shipped migration helper and the shipped store:

```python
run_alembic_upgrade('postgres', dsn=...)   # -> ok
store.register_run(run_id, tenant_id)      # -> psycopg.errors.InvalidColumnReference
```

```
                     Partitioned table "public.runs"
Partition key: RANGE (started_at)
    "runs_pkey" PRIMARY KEY, btree (run_id, started_at)
Number of partitions: 16
```

Impact, in order of severity:

1. **The MetadataStore Security Gate cannot return a verdict.** That job exists to
   demonstrate cross-tenant isolation and RLS hold. While this is open, the
   project has **no CI evidence that tenant isolation works** — not because
   isolation is broken, but because the tests that would show it cannot run.
2. Server mode cannot index runs after migrating.
3. The Postgres half of the `unit` job fails (#21).

It went unseen because the only test exercising this path is gated on
`pg_dump`/`pg_restore` being installed — absent on a dev box, present on CI
runners — and the CI job that would have run it was hitting a 15-minute timeout
for five weeks.

**Local mode is unaffected**; SQLite does not partition.

## Detailed design

### The disagreement

Three sources define `runs`, and `CREATE TABLE IF NOT EXISTS` means the first to
run wins silently:

| Source | Shape | Unique constraint |
|---|---|---|
| `_DDL_SQL` in `metadata_store/postgres.py` | un-partitioned | `(run_id, tenant_id)` |
| repo `alembic/postgres/` | does not create `runs` at all | — |
| packaged `metadata_store/migrations/postgres/` | **partitioned** on `started_at` | `(run_id, started_at)` |

Both halves are internally consistent. Postgres *requires* the partition key in
any unique constraint on a partitioned table, so `v002_partition_ddl` had no
choice given ADR-0051's `PARTITION BY RANGE (started_at)`. And
`MetadataStore.register_run`'s docstring states the intended contract plainly:
*"Idempotent on (run_id, tenant_id)"*.

### What has to be decided

**Does idempotency mean "one row per run per tenant", or "one row per run per
tenant per start time"?** Everything else follows.

This is security-relevant, not merely a correctness question: `runs` carries
`tenant_id` and is covered by the `tenant_isolation` RLS policy, so any change to
its keying should be reviewed against cross-tenant leakage, not only against
"does the insert succeed".

### Options

**A. `ON CONFLICT (run_id, started_at)`** — match the partitioned key.
*Smallest diff, no migration.* Weakens the contract: the same `run_id`
re-registered with a different `started_at` inserts a second row. Whether that is
reachable depends on whether `started_at` is derived deterministically from the
run — **someone needs to confirm that before this option is safe**, and it is not
confirmed here.

**B. Bare `ON CONFLICT DO NOTHING`** — legal on partitioned tables, preserves
"insert if absent", no migration. But it swallows *any* constraint violation, not
only the intended one, which is the kind of broadening that hides a future bug.

**C. `PRIMARY KEY (run_id, tenant_id, started_at)` + `ON CONFLICT` on all three.**
Keeps `tenant_id` in the key, so tenant scoping is structural rather than
incidental. Costs a migration over a partitioned table with 16 partitions, and
partition pruning must be re-measured. Closest to preserving both stated
guarantees, and the option this RFC leans toward — but "leans toward" is not a
decision, and the pruning evidence has not been re-read.

**D. Revert partitioning.** ADR-0051 accepted it on smoke-test evidence
(`bench/rls_partition_pruning/`). Largest change; only justified if that evidence
does not hold up.

### Required regardless of choice

`bootstrap()` must **fail loudly** when it finds an existing `runs` whose
constraints do not match what the code is about to rely on. Today
`CREATE TABLE IF NOT EXISTS` silently accepts a mismatched table and the failure
surfaces several calls later as an opaque `InvalidColumnReference`. That silence
is what let three schema definitions coexist unnoticed.

## Drawbacks

Options A and B change a documented guarantee that downstream code may rely on
without saying so. Option C requires migrating a partitioned table, which is the
riskiest kind of migration to get wrong. Option D discards accepted work.

There is no zero-cost option, which is why this is an RFC and not a patch.

## Alternatives considered

Leaving it and skipping the affected tests — rejected. The affected tests are the
tenant-isolation gate; skipping them removes the evidence rather than the defect,
on a project whose entire premise is verifiable evidence.

## Prior art

Postgres's own requirement that a unique constraint on a partitioned table
include the partition key is the root constraint; this is a well-known tension in
multi-tenant partitioned schemas, usually resolved by making the tenant column
part of either the partition key or the composite unique key (option C).

## Unresolved questions

1. Is `started_at` deterministic for a given run? **This decides whether option A
   is safe**, and it is unanswered.
2. Does ADR-0051's pruning benefit survive a three-column key?
3. Should the repo `alembic/postgres/` tree own `runs` at all, or should
   `_DDL_SQL` be retired in favour of migrations as the single source?

## Adoption / migration plan

To be completed once an option is chosen. Any option except A and B needs a
forward migration and a documented path for existing deployments.

## Security and threat-model impact

`runs` is RLS-protected and tenant-scoped. A keying change must be reviewed for
cross-tenant collision, and the isolation tests must pass **before** this is
considered resolved — they are currently the thing that cannot run.

## Decision log

| Date | Author | Note |
|---|---|---|
| 2026-08-05 | investigation from #23 | Initial draft. Filed rather than patched: this is a security-relevant change to a documented guarantee, which `docs/governance/rfc-process.md` requires go through an RFC with a comment window and two approvers. |
