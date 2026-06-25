#!/bin/sh
# NovaFabric Slurm Epilog — flushes the per-job NATS leaf node within timeout.
# ALWAYS exits 0. Install: set Epilog=/path/to/epilog.sh in slurm.conf
#
# Environment variables (all optional, with defaults):
#   NOVAFABRIC_EPILOG_FLUSH_TIMEOUT  Seconds to wait for spool flush (default: 50)
#   NOVAFABRIC_SPOOL_BASE            Base directory for per-job spools (default: /tmp/novafabric)
#   NOVAFABRIC_HPC_STORAGE           Set to "lustre" to use Lustre-based spool path
#   LUSTRE_SPOOL_PATH                Lustre mount point (required if NOVAFABRIC_HPC_STORAGE=lustre)
#   NOVAFABRIC_CLUSTER_ID            Cluster identifier string (default: local)
#
# Exit codes: ALWAYS 0. A non-zero epilog exit causes Slurm to mark the node
# as failed (slurmctld drain), which is never the right response to a telemetry
# flush timeout.

FLUSH_TIMEOUT="${NOVAFABRIC_EPILOG_FLUSH_TIMEOUT:-50}"
NOVAFABRIC_SPOOL_BASE="${NOVAFABRIC_SPOOL_BASE:-/tmp/novafabric}"
NOVAFABRIC_CLUSTER_ID="${NOVAFABRIC_CLUSTER_ID:-local}"

# Determine spool directory (must match prolog.sh logic)
if [ "${NOVAFABRIC_HPC_STORAGE}" = "lustre" ] && [ -n "${LUSTRE_SPOOL_PATH}" ]; then
    SPOOL_DIR="${LUSTRE_SPOOL_PATH}/${SLURM_CLUSTER_NAME:-cluster}/${SLURMD_NODENAME:-node}/${SLURM_JOB_ID}"
else
    SPOOL_DIR="${NOVAFABRIC_SPOOL_BASE}/${SLURM_JOB_ID}"
fi

# Attempt NATS stream flush — log result but never fail
STREAM_NAME="novafabric-${SLURM_JOB_ID}"
if command -v nats > /dev/null 2>&1; then
    nats stream flush --timeout="${FLUSH_TIMEOUT}s" "${STREAM_NAME}" \
        >> "${SPOOL_DIR}/epilog.log" 2>&1 || true
else
    echo "$(date -u +%FT%TZ) [epilog] nats CLI not found; skipping stream flush for ${STREAM_NAME}" \
        >> "${SPOOL_DIR}/epilog.log" 2>&1 || true
fi

# Stop the hpc-hub leaf node using the PID file
PID_FILE="${SPOOL_DIR}/hpc-hub.pid"
if [ -f "${PID_FILE}" ]; then
    HUB_PID=$(cat "${PID_FILE}" 2>/dev/null || true)
    if [ -n "${HUB_PID}" ]; then
        kill -TERM "${HUB_PID}" 2>/dev/null || true
        # Give it up to 5 seconds to shut down gracefully
        WAIT=0
        while kill -0 "${HUB_PID}" 2>/dev/null && [ "${WAIT}" -lt 5 ]; do
            sleep 1
            WAIT=$((WAIT + 1))
        done
        # Force kill if still running
        kill -KILL "${HUB_PID}" 2>/dev/null || true
    fi
    rm -f "${PID_FILE}" || true
fi

# ALWAYS exit 0 — never let telemetry failure drain the node
exit 0
