#!/bin/sh
set -e

# Ensure bind-mount directories exist (first boot on a fresh host path)
mkdir -p /data/capsules /data/nova /data/kuzu

PGHOST="${PGHOST:-postgres}"
PGUSER="${PGUSER:-nova}"

echo "[nova] waiting for postgres at ${PGHOST}..."
until pg_isready -h "$PGHOST" -U "$PGUSER" -q; do
    sleep 1
done
echo "[nova] postgres ready."

echo "[nova] running schema migrations (nova db upgrade --backend postgres --revision v001)..."
# Stop at v001: v002 (partition DDL) is gated behind the cap-003 benchmark.
nova db upgrade --backend postgres --revision v001

echo "[nova] starting dashboard on 0.0.0.0:4321 ..."
exec nova serve --experimental \
    --host 0.0.0.0 \
    --insecure \
    --no-browser \
    --topology \
    --tv5 \
    --capsule-dir /data/capsules \
    --db-path /data/nova/registry.db \
    --port 4321
