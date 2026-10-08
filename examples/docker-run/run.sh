#!/usr/bin/env bash
# Capture payload.py running inside a stock python:3.12-slim container.
#
# Exits 0 with a skip message when Docker is unavailable — no `docker` binary,
# or a daemon that does not answer. CI runners and many first-time clones have
# no Docker, and that must not read as a failure of the example.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out_dir="${1:-${here}/capsules}"
image="python:3.12-slim"   # stock, public, no build step

if ! command -v docker >/dev/null 2>&1; then
  echo "skip: 'docker' is not on PATH — this example needs a Docker daemon."
  exit 0
fi
if ! docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
  echo "skip: the Docker daemon is not reachable ('docker info' failed)."
  echo "      Start Docker, or check that your user may talk to the daemon."
  exit 0
fi

mkdir -p "${out_dir}"

# user=<your uid>:<your gid> runs the workload as YOU, not as the image's
# default root user: nothing in the container runs privileged, and anything the
# workload writes into the bind-mounted capsule stays owned by you on the host.
nova capture \
  --output-dir "${out_dir}" \
  --runner docker \
  --runner-option "image=${image}" \
  --runner-option "user=$(id -u):$(id -g)" \
  --runner-option workdir=/work \
  --runner-option "extra_volumes=${here}:/work:ro" \
  -- python /work/payload.py

echo "capsule written under ${out_dir}"
