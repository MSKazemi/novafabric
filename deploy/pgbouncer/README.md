# PgBouncer — NovaFabric Production Config

PgBouncer sits between the NovaFabric server and Postgres, providing connection
pooling for the MetadataStore (ADR-0040) and server-mode API (ADR-0027/ADR-0029).

## Why PgBouncer

NovaFabric uses SQLAlchemy async sessions with a per-request connection checkout
pattern. Without pooling, a burst of 100 concurrent API requests would open 100
Postgres connections — exhausting `max_connections` on a default Postgres install.
PgBouncer with `transaction` pooling multiplexes those 100 clients onto
`default_pool_size=20` backend connections.

## Quick start

```bash
# 1. Install
apt-get install pgbouncer   # or brew install pgbouncer

# 2. Configure
cp pgbouncer.ini /etc/pgbouncer/pgbouncer.ini
cp userlist.txt.template /etc/pgbouncer/userlist.txt
# Edit both files — fill in <PLACEHOLDER> values.

# 3. Start
systemctl start pgbouncer

# 4. Verify
psql -h 127.0.0.1 -p 5432 -U pgbouncer pgbouncer -c "SHOW POOLS;"
```

## Docker Compose usage

```yaml
services:
  pgbouncer:
    image: bitnami/pgbouncer:latest
    environment:
      PGBOUNCER_DATABASE: novafabric
      PGBOUNCER_POOL_MODE: transaction
      PGBOUNCER_MAX_CLIENT_CONN: "300"
      PGBOUNCER_DEFAULT_POOL_SIZE: "20"
      POSTGRESQL_HOST: postgres
      POSTGRESQL_PORT: "5432"
      POSTGRESQL_USERNAME: novafabric_app
      POSTGRESQL_PASSWORD: "${NOVAFABRIC_DB_PASSWORD}"
      POSTGRESQL_DATABASE: novafabric
    ports:
      - "5432:5432"
    depends_on:
      - postgres
```

Set `NOVA_DSN=postgresql+asyncpg://novafabric_app:<pw>@pgbouncer:5432/novafabric`
in the NovaFabric server environment.

## Role split

NovaFabric uses two Postgres roles (ADR-0040 §3, `test_role_split.py`):

| Role | Purpose | PgBouncer user |
|---|---|---|
| `novafabric_app` | Normal query execution — NOBYPASSRLS | Yes |
| `novafabric_migrator` | Schema migrations — BYPASSRLS | Yes (migration-only sessions) |

The `novafabric_migrator` role must never be used by the application pool.
Run migrations via a separate one-shot connection (`nova migrate-to-postgres`).

## Security notes

- `auth_type = scram-sha-256` — do not use `trust` or `md5` in production.
- TLS between PgBouncer and Postgres — see commented `server_tls_*` lines in
  `pgbouncer.ini`. Required if PgBouncer and Postgres are on different hosts.
- The `userlist.txt` file contains password verifiers — restrict to mode 0600,
  owner pgbouncer. **Never commit with real verifiers.**
- `listen_addr = 127.0.0.1` — do not expose PgBouncer directly to the internet.
  Place behind the NovaFabric server or a private network.

## Monitoring

`pgbouncer_exporter` (Prometheus, Apache-2.0) exposes PgBouncer stats at `:9127/metrics`.
Key metrics to alert on: `pgbouncer_pools_sv_idle`, `pgbouncer_pools_cl_waiting`.
