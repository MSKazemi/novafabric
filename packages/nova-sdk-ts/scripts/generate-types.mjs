#!/usr/bin/env node
/**
 * Generate src/types.gen.ts from ../../api/openapi.yaml (the single source
 * of truth for the /v0 REST contract — ADR-0194 D1).
 *
 * Why this script exists instead of a plain
 * `npx openapi-typescript ../../api/openapi.yaml -o src/types.gen.ts`:
 *
 * PRE-EXISTING SPEC BUG WORKAROUND. api/openapi.yaml's /admin/flush-jwks
 * path references `#/components/responses/Unauthorized` and
 * `#/components/responses/Forbidden`, but neither response is defined in
 * `components.responses`. openapi-typescript v7 bundles the spec with
 * Redocly before generation and a dangling $ref is a fatal error, so the
 * plain CLI invocation cannot succeed until the YAML is fixed.
 *
 * This script loads the YAML, injects ONLY the two missing responses
 * (standard error-envelope responses, exactly like the sibling entries the
 * YAML already defines), and generates types programmatically. Everything
 * else is passed through untouched. Once api/openapi.yaml defines
 * Unauthorized/Forbidden itself, the patch below becomes a no-op and can be
 * deleted (the script warns while the patch is still active).
 *
 * Usage: node scripts/generate-types.mjs [output-path]
 */
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { load as loadYaml } from "js-yaml";
import openapiTS, { astToString } from "openapi-typescript";

const here = path.dirname(fileURLToPath(import.meta.url));
const specPath = path.resolve(here, "..", "..", "..", "api", "openapi.yaml");
const outPath = process.argv[2]
  ? path.resolve(process.cwd(), process.argv[2])
  : path.resolve(here, "..", "src", "types.gen.ts");

const doc = loadYaml(readFileSync(specPath, "utf8"));

const errorEnvelopeResponse = (description) => ({
  description,
  content: {
    "application/json": {
      schema: { $ref: "#/components/schemas/ErrorEnvelope" },
    },
  },
});

const responses = doc.components?.responses ?? {};
const patched = [];
if (!responses.Unauthorized) {
  responses.Unauthorized = errorEnvelopeResponse(
    "Missing or invalid bearer token.",
  );
  patched.push("Unauthorized");
}
if (!responses.Forbidden) {
  responses.Forbidden = errorEnvelopeResponse(
    "Authenticated but not permitted (missing role).",
  );
  patched.push("Forbidden");
}
if (patched.length > 0) {
  console.warn(
    `[generate-types] patched missing components.responses in api/openapi.yaml: ` +
      `${patched.join(", ")} — pre-existing spec bug (referenced by ` +
      `/admin/flush-jwks but never defined). Remove this workaround once the ` +
      `YAML defines them.`,
  );
}

const ast = await openapiTS(doc);
const banner = `/**
 * This file was auto-generated from api/openapi.yaml by
 * scripts/generate-types.mjs (openapi-typescript). Do not edit by hand —
 * run \`npm run generate:types\` instead. Drift is gated by
 * \`npm run check:drift\`.
 */
`;
writeFileSync(outPath, banner + astToString(ast));
console.log(`[generate-types] wrote ${outPath}`);
