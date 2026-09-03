#!/usr/bin/env node
/**
 * Generate src/types.gen.ts from ../../api/openapi.yaml (the single source
 * of truth for the /v0 REST contract — ADR-0194 D1).
 *
 * This script used to carry a workaround for a dangling `$ref`: the spec's
 * /admin/flush-jwks path referenced `#/components/responses/Unauthorized` and
 * `Forbidden` without defining either, and openapi-typescript v7 bundles with
 * Redocly, for which a dangling $ref is fatal. The spec is now generated from
 * the server app (scripts/gen_openapi.py) and has no `components.responses`
 * section and no dangling references at all, so the workaround was removed —
 * it had also stopped doing anything, having patched a copy of the section
 * rather than the document, while still printing its warning on every run.
 *
 * A dangling $ref reaching this point should fail loudly rather than be
 * silently repaired: it means the server is describing something it does not
 * define.
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
