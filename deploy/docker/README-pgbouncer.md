# pgBouncer Setup for NovaFabric

pgBouncer 1.24 sits between the NovaFabric application and Postgres in the
production Docker Compose stack.  It is part of the `prod` profile and is
optional for local development.

---

## Quick start

```bash
# Start the full production stack (includes pgBouncer on port 6432):
docker compose --profile prod up -d pgbouncer postgres

# Connect directly through pgBouncer to verify:
psql "postgresql://novafabric_app:<password>@localhost:6432/novafabric"
```

---

## Configuration files

| File | Purpose |
|---|---|
| `pgbouncer.ini` | Main pgBouncer config (pool mode, limits, TLS) |
| `pgbouncer-userlist.txt` | SCRAM-SHA-256 password hashes (template — fill before deploy) |

Both files are mounted read-only into the container:

```yaml
volumes:
  - ./pgbouncer.ini:/etc/pgbouncer/pgbouncer.ini:ro
  - ./pgbouncer-userlist.txt:/etc/pgbouncer/userlist.txt:ro
```

---

## Generating SCRAM-SHA-256 password hashes

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
Postgres (port 5432):

```bash
# MetadataStore
export NOVAFABRIC_METADATA_DSN="postgresql+asyncpg://novafabric_app:<password>@pgbouncer:6432/novafabric"

# Legacy registry DSN (nova migrate-to-postgres)
export NOVAFABRIC_POSTGRES_DSN="postgresql://novafabric_app:<password>@pgbouncer:6432/novafabric"
```

---

## Reloading after password rotation

```bash
# Reload pgBouncer config without dropping active connections:
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
