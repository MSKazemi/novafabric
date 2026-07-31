#!/bin/sh
set -e

# Ensure bind-mount directories exist (first boot on a fresh host path)
mkdir -p /data/capsules /data/nova /data/kuzu

PGHOST="${PGHOST:-postgres}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-nova}"

# NOVA_MODE selects what this container runs:
#   dashboard (default) — `nova serve`, the experimental read-only dashboard
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

echo "[nova] running schema migrations (nova db upgrade --backend postgres --revision ${NOVA_DB_REVISION})..."
nova db upgrade --backend postgres --revision "$NOVA_DB_REVISION"

if [ "$NOVA_MODE" = "server" ]; then
    NOVA_PORT="${NOVA_PORT:-7433}"
    echo "[nova] starting multi-user REST API server (OIDC/RBAC/Postgres) on 0.0.0.0:${NOVA_PORT} [workers=${NOVA_WORKERS:-1}] ..."
    exec nova server start \
        --backend postgres \
        --host 0.0.0.0 \
        --port "${NOVA_PORT}" \
        --workers "${NOVA_WORKERS:-1}"
fi

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
