#!/bin/bash
#SBATCH --job-name=novafabric-example
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=00:05:00
#
# Capture a Slurm batch job as a NovaFabric Run Capsule.
#
# The pattern is the whole point and it is deliberately boring: `nova capture`
# wraps the payload *inside* the batch script. Slurm schedules the job; NovaFabric
# captures what the job did. Neither needs to know about the other, which is why
# this works on any scheduler without a plugin.
#
# Submit with:   sbatch job.sbatch
# Run locally:   ./job.sbatch          (see "Without a scheduler" in the README)

set -euo pipefail

# Where capsules land. On a cluster this MUST be a shared filesystem the login
# node can also read — /tmp on a compute node is gone by the time you look.
#
# --output-dir, not NOVAFABRIC_HOME: a config file can override the environment
# variable, and a batch job that silently writes its evidence somewhere else is
# the one failure mode this example cannot afford. The flag is unambiguous.
CAPSULE_DIR="${NOVAFABRIC_CAPSULE_OUT:-${SLURM_SUBMIT_DIR:-$PWD}/novafabric-capsules}"
mkdir -p "${CAPSULE_DIR}"

# Where the payload lives. This line is the one non-obvious thing in the file.
#
# `dirname "${BASH_SOURCE[0]}"` is WRONG inside a batch job: Slurm copies the
# script to a per-job spool directory on the compute node before running it, so
# it resolves to something like /var/spool/slurmd/job00001 and the payload is not
# there. Measured on a real single-node Slurm 23.x cluster, not guessed.
#
# SLURM_SUBMIT_DIR is the directory you ran `sbatch` from, which is where the
# payload actually is. The fallback keeps the script runnable as a plain shell
# script with no scheduler present.
if [ -n "${SLURM_SUBMIT_DIR:-}" ]; then
    SCRIPT_DIR="${SLURM_SUBMIT_DIR}"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

# --environment is set explicitly: `nova validate` warns for values outside
# {development, test, staging, production}, and a warning on every job only
# teaches people to ignore warnings.
#
# Note there is deliberately no --experiment here. That flag is A/B *attribution*
# (ADR-0116) and requires --variant and --variant-source with it; it is not a
# label for the job. Using it as one fails at the CLI.
nova capture \
    --output-dir "${CAPSULE_DIR}" \
    --environment production \
    -- \
    python3 "${SCRIPT_DIR}/payload.py"

echo "job.sbatch: capsules are under ${CAPSULE_DIR}"
