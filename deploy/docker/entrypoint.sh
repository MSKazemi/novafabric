#!/bin/sh
set -e

# Ensure bind-mount directories exist (first boot on a fresh host path)
mkdir -p /data/capsules /data/nova /data/kuzu

PGHOST="${PGHOST:-postgres}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-nova}"

# NOVA_MODE selects what this container runs:
#   dashboard — `nova serve`, the experimental dashboard. NOT read-only: it
#               exposes irreversible operations (ADR-0230).
#                         (HTTP + printed token; front with TLS). Unchanged.
#   server              — `nova server start`, the multi-user REST API with
#                         OIDC/RBAC over Postgres. Auth is ON by default; set
#                         OIDC via NOVAFABRIC_SERVER_* env, else a local bearer
#                         token is generated. Never runs --insecure-no-auth.
NOVA_MODE="${NOVA_MODE:-dashboard}"

# Migrate to the packaged script head by default. The server refuses to start
# when the schema is stamped behind head (SchemaSkewError), so a pinned-below
# revision breaks the deploy; operators who must pin can override NOVA_DB_REVISION.
NOVA_DB_REVISION="${NOVA_DB_REVISION:-head}"

echo "[nova] waiting for postgres at ${PGHOST}:${PGPORT}..."
until pg_isready -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -q; do
    sleep 1
done
echo "[nova] postgres ready."

if [ "$NOVA_MODE" = "server" ]; then
    # Server mode migrates the REGISTRY track, the one `nova server start`'s
    # schema-skew guard checks against NOVAFABRIC_POSTGRES_DSN. It must not
    # also run the MetadataStore track (`nova db upgrade`'s default): both
    # tracks keep their stamp in the same `alembic_version` table, and in the
    # bundled compose stack both DSNs name the same database, so the metadata
    # head (v005) would overwrite the registry stamp and the server would
    # refuse to start (SchemaSkewError: v005 is not known to this build). The
    # server creates the MetadataStore tables it uses itself on first use.
    echo "[nova] running schema migrations (nova db upgrade --track registry --backend postgres --revision ${NOVA_DB_REVISION})..."
    nova db upgrade --track registry --backend postgres --revision "$NOVA_DB_REVISION"
    NOVA_PORT="${NOVA_PORT:-7433}"
    echo "[nova] starting multi-user REST API server (OIDC/RBAC/Postgres) on 0.0.0.0:${NOVA_PORT} [workers=${NOVA_WORKERS:-1}] ..."
    exec nova server start \
        --backend postgres \
        --host 0.0.0.0 \
        --port "${NOVA_PORT}" \
        --workers "${NOVA_WORKERS:-1}"
fi

# Dashboard mode (`nova serve`, SQLite registry) keeps the MetadataStore track.
echo "[nova] running schema migrations (nova db upgrade --backend postgres --revision ${NOVA_DB_REVISION})..."
nova db upgrade --backend postgres --revision "$NOVA_DB_REVISION"

echo "[nova] starting dashboard on 0.0.0.0:${NOVA_PORT:-4321} ..."
exec nova serve --experimental \
    --host 0.0.0.0 \
    --insecure \
    --no-browser \
    --topology \
    --tv5 \
    --capsule-dir /data/capsules \
    --db-path /data/nova/registry.db \
    --port "${NOVA_PORT:-4321}"
