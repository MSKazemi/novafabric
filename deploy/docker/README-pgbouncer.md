# pgBouncer Setup for NovaFabric

pgBouncer 1.25.2 (`edoburu/pgbouncer:v1.25.2-p0`, the pin recorded in
`deploy/IMAGE_PINS.md`) sits between the NovaFabric application and Postgres in
the production Docker Compose stack.  It is part of the `prod` profile and is
optional for local development.

> **Note:** the compose `pgbouncer` service (`deploy/docker/docker-compose.yml`,
> `prod` profile) configures itself purely through `edoburu/pgbouncer`
> environment variables (`DB_HOST`, `DB_PORT`, `POOL_MODE`, etc.) and mounts no
> `.ini` file into the container at all. `pgbouncer.ini` in this directory is
> reference documentation for anyone running pgBouncer outside compose (e.g. a
> bare-metal or systemd deployment) — it is not live configuration that
> compose actually reads. Wiring it in is tracked as future work, out of scope
> here. The same service also sets `AUTH_TYPE: trust`, so the
> SCRAM-SHA-256 / `pgbouncer-userlist.txt` workflow this file documents below
> does not govern its client authentication either — for a second, independent
> reason beyond the file not being mounted. Any instructions below that talk
> about mounting `pgbouncer.ini`/`pgbouncer-userlist.txt` or reloading them
> inside "the" pgBouncer container describe a **standalone** (non-compose)
> pgBouncer deployment, not this repo's `prod` service.

---

## Quick start

```bash
# Start the full production stack (includes pgBouncer on port 6432):
docker compose --profile prod up -d pgbouncer postgres

# Connect directly through pgBouncer to verify. This repo's compose service
# is configured with DB_NAME=nova/DB_USER=nova/DB_PASSWORD=nova; AUTH_TYPE:
# trust means pgBouncer does not check the password, but the database name
# must still be "nova" to route correctly:
psql "postgresql://nova:nova@localhost:6432/nova"
```

---

## Configuration files

| File | Purpose |
|---|---|
| `pgbouncer.ini` | Main pgBouncer config (pool mode, limits, TLS) |
| `pgbouncer-userlist.txt` | SCRAM-SHA-256 password hashes (template — fill before deploy) |

As the note above states, the `prod`-profile compose service does **not**
mount either file — it configures itself from environment variables instead.
If you run pgBouncer standalone (outside this compose stack, e.g. bare-metal
or your own container), these are the two files to mount read-only:

```yaml
volumes:
  - ./pgbouncer.ini:/etc/pgbouncer/pgbouncer.ini:ro
  - ./pgbouncer-userlist.txt:/etc/pgbouncer/userlist.txt:ro
```

---

## Generating SCRAM-SHA-256 password hashes

> Not used by this repo's compose `prod` service today — see the note at the
> top of this file (`AUTH_TYPE: trust`, no mounted userlist). This section
> documents the workflow for a standalone pgBouncer deployment.

pgBouncer requires pre-computed SCRAM-SHA-256 hashes, not plaintext passwords.

**Step 1 — Create the Postgres roles:**

```sql
-- Run as superuser on your Postgres server
CREATE ROLE novafabric_app      WITH LOGIN PASSWORD 'your-app-password';
CREATE ROLE novafabric_migrator WITH LOGIN PASSWORD 'your-migrator-password';
CREATE ROLE pgbouncer_admin     WITH LOGIN PASSWORD 'your-admin-password';
CREATE ROLE pgbouncer_stats     WITH LOGIN PASSWORD 'your-stats-password';
```

> Requires `password_encryption = scram-sha-256` (default in Postgres 14+).
> Check with: `SHOW password_encryption;`

**Step 2 — Retrieve the stored hashes:**

```sql
SELECT rolname, rolpassword
  FROM pg_authid
 WHERE rolname IN (
   'novafabric_app', 'novafabric_migrator',
   'pgbouncer_admin', 'pgbouncer_stats'
 );
```

**Step 3 — Update `pgbouncer-userlist.txt`:**

Replace each `SCRAM-SHA-256$<iterations>:<salt>$<stored-key>:<server-key>`
placeholder with the actual string returned by `rolpassword`.

---

## Connection string format

Point your NovaFabric installation at pgBouncer (port **6432**) instead of
Postgres (port 5432). Against this repo's compose service
(`DB_NAME=nova`/`DB_USER=nova`/`DB_PASSWORD=nova`, `AUTH_TYPE: trust`):

```bash
# MetadataStore
export NOVAFABRIC_METADATA_DSN="postgresql+asyncpg://nova:nova@pgbouncer:6432/nova"

# Legacy registry DSN (nova migrate-to-postgres)
export NOVAFABRIC_POSTGRES_DSN="postgresql://nova:nova@pgbouncer:6432/nova"
```

For a standalone pgBouncer deployment with your own Postgres roles and
database (e.g. the `novafabric_app`/`novafabric_migrator` split from
"Generating SCRAM-SHA-256 password hashes" above), substitute your own
role, password, and database name in place of `nova:nova@.../nova`.

---

## Reloading after password rotation

This repo's compose `prod` service has no mounted `userlist.txt`/`pgbouncer.ini`
to reload — it uses `AUTH_TYPE: trust` and reads its settings from
`environment:` in `docker-compose.yml`. To change them, edit that block and
recreate the service:

```bash
docker compose --profile prod up -d --force-recreate pgbouncer
```

The command below applies to a **standalone** pgBouncer deployment where you
have mounted `pgbouncer.ini`/`userlist.txt` yourself (your own compose file
or container) — it reloads config without dropping active connections:

```bash
docker compose exec pgbouncer pgbouncer -R /etc/pgbouncer/pgbouncer.ini
```

---

## Pool sizing guidance

The defaults in `pgbouncer.ini` are tuned for a medium-scale deployment
(≤ 50 concurrent Nova workers).  For larger deployments:

| Workers | `default_pool_size` | `max_client_conn` |
|---|---|---|
| ≤ 50 | 20 (default) | 1000 (default) |
| 51–200 | 40 | 2000 |
| 201–500 | 80 | 5000 |

Postgres `max_connections` must be at least `default_pool_size + reserve_pool_size + 5`.

---

## Monitoring

`pgbouncer_exporter` (Prometheus, Apache-2.0) exposes pgBouncer stats scraped
via the admin/stats console (`stats_users` in `pgbouncer.ini`) at
`:9127/metrics`. Key metrics to alert on: `pgbouncer_pools_sv_idle`,
`pgbouncer_pools_cl_waiting`.
