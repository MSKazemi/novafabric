#!/usr/bin/env bash
# Capture a whole notebook execution as one Run Capsule.
#
# Exits 0 with a skip message when the Jupyter toolchain is absent: nbconvert is
# a dependency of this *example*, never of NovaFabric, so a clean checkout must
# not fail here.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out_dir="${1:-${here}/capsules}"

if ! command -v jupyter >/dev/null 2>&1; then
  echo "skip: 'jupyter' is not on PATH — install the example's extra with:"
  echo "      pip install nbconvert ipykernel"
  echo "      (nbconvert is an extra for this example only, not a NovaFabric dependency)"
  exit 0
fi

mkdir -p "${out_dir}"

# The notebook honours this; without it a cell writes results.json into the
# example's own directory, dirtying the checkout it was run from.
export NOTEBOOK_OUTPUT_DIR="${out_dir}"

# --to notebook --execute runs every cell in a real kernel and writes the
# executed copy, so the capsule's outputs carry the executed notebook too.
nova capture \
  --output-dir "${out_dir}" \
  -- jupyter nbconvert \
       --to notebook \
       --execute "${here}/analysis.ipynb" \
       --output-dir "${out_dir}" \
       --output executed.ipynb

echo "capsule written under ${out_dir}"
