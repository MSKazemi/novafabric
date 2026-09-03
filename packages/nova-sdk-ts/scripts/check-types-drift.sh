#!/usr/bin/env bash
# Drift gate (ADR-0194 D1): regenerate the OpenAPI types to a temp file and
# diff against the committed src/types.gen.ts. Exits 1 on drift so CI fails
# whenever api/openapi.yaml changes without a matching regeneration.
set -euo pipefail

cd "$(dirname "$0")/.."

tmp="$(mktemp --suffix=.types.gen.ts)"
trap 'rm -f "$tmp"' EXIT

node scripts/generate-types.mjs "$tmp" >/dev/null

if ! diff -u src/types.gen.ts "$tmp"; then
  echo "" >&2
  echo "ERROR: src/types.gen.ts is out of sync with api/openapi.yaml." >&2
  echo "Run 'npm run generate:types' in packages/nova-sdk-ts and commit the result." >&2
  exit 1
fi

echo "OK: src/types.gen.ts is in sync with api/openapi.yaml."
