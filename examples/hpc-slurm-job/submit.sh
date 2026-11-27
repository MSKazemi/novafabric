#!/usr/bin/env bash
# Submit job.sbatch to Slurm — or say plainly that there is no Slurm here.
#
# Exits 0 with a skip message when `sbatch` is not on PATH: a laptop, a CI
# runner and a first-time clone have no scheduler, and that must not read as a
# failure. The capture pattern itself does not need Slurm at all — see
# "Without a scheduler" in README.md.
set -euo pipefail

# This script's own directory, resolved with shell built-ins only: `dirname` is
# a coreutils binary, and the skip below must work on a PATH that has none. A
# symlinked script is followed to its target (with `readlink`, when present).
src="${BASH_SOURCE[0]}"
hops=0
while [ -L "${src}" ] && [ "${hops}" -lt 40 ] && command -v readlink >/dev/null 2>&1; do
  link="$(readlink "${src}")"
  case "${link}" in
    /*) src="${link}" ;;
    *) case "${src}" in */*) src="${src%/*}/${link}" ;; *) src="${link}" ;; esac ;;
  esac
  hops=$((hops + 1))
done
case "${src}" in
  */*) here="${src%/*}" ;;
  *) here="." ;;
esac
here="$(CDPATH='' cd -P -- "${here:-/}" && pwd -P)"

if ! command -v sbatch >/dev/null 2>&1; then
  echo "skip: 'sbatch' is not on PATH — no Slurm scheduler on this machine."
  echo "      To see the same capture without a scheduler, run either of:"
  echo "        ${here}/job.sbatch"
  echo "        nova capture -- python3 ${here}/payload.py"
  exit 0
fi

# sbatch sets SLURM_SUBMIT_DIR to the directory it is invoked from, and
# job.sbatch resolves payload.py from there — so submit from the example dir.
cd "${here}"
sbatch job.sbatch
