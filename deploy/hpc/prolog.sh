#!/bin/sh
# NovaFabric Slurm Prolog — initializes per-job evidence spool and NATS leaf node.
# Install: set Prolog=/path/to/prolog.sh and PrologFlags=Alloc in slurm.conf
#
# Environment variables (all optional, with defaults):
#   NOVAFABRIC_SPOOL_BASE       Base directory for per-job spools (default: /tmp/novafabric)
#   NOVAFABRIC_HPC_STORAGE      Set to "lustre" to use Lustre-based spool path
#   LUSTRE_SPOOL_PATH           Lustre mount point for spool (required if NOVAFABRIC_HPC_STORAGE=lustre)
#   NOVAFABRIC_HUB_ADDRESS      Address of the NATS cluster hub (default: localhost:4222)
#   NOVAFABRIC_CLUSTER_ID       Cluster identifier string (default: local)
#   NOVAFABRIC_ENV              Runtime environment label (default: hpc)
#
# Exit codes: always 0 (Slurm requires prolog success to proceed with job).

set -e

NOVAFABRIC_SPOOL_BASE="${NOVAFABRIC_SPOOL_BASE:-/tmp/novafabric}"
NOVAFABRIC_HUB_ADDRESS="${NOVAFABRIC_HUB_ADDRESS:-localhost:4222}"
NOVAFABRIC_CLUSTER_ID="${NOVAFABRIC_CLUSTER_ID:-local}"
NOVAFABRIC_ENV="${NOVAFABRIC_ENV:-hpc}"

# Determine spool directory
if [ "${NOVAFABRIC_HPC_STORAGE}" = "lustre" ] && [ -n "${LUSTRE_SPOOL_PATH}" ]; then
    SPOOL_DIR="${LUSTRE_SPOOL_PATH}/${SLURM_CLUSTER_NAME:-cluster}/${SLURMD_NODENAME:-node}/${SLURM_JOB_ID}"
else
    SPOOL_DIR="${NOVAFABRIC_SPOOL_BASE}/${SLURM_JOB_ID}"
fi

mkdir -p "${SPOOL_DIR}"

# Write per-job configuration file
cat > "${SPOOL_DIR}/novafabric-job.conf" <<CONF
job_id=${SLURM_JOB_ID}
cluster_id=${NOVAFABRIC_CLUSTER_ID}
node_id=${SLURMD_NODENAME:-unknown}
hub_address=${NOVAFABRIC_HUB_ADDRESS}
spool_dir=${SPOOL_DIR}
env=${NOVAFABRIC_ENV}
CONF

# Export spool path for child processes
export NOVAFABRIC_JOB_SPOOL="${SPOOL_DIR}"

# Start novafabric-hpc-hub leaf node if the binary is available
if command -v novafabric-hpc-hub > /dev/null 2>&1; then
    novafabric-hpc-hub \
        --job-id "${SLURM_JOB_ID}" \
        --spool "${SPOOL_DIR}" \
        --hub "${NOVAFABRIC_HUB_ADDRESS}" \
        --cluster "${NOVAFABRIC_CLUSTER_ID}" \
        --node "${SLURMD_NODENAME:-unknown}" \
        --pid-file "${SPOOL_DIR}/hpc-hub.pid" \
        > "${SPOOL_DIR}/hpc-hub.log" 2>&1 &
    HUB_PID=$!
    echo "${HUB_PID}" > "${SPOOL_DIR}/hpc-hub.pid"
fi

exit 0
