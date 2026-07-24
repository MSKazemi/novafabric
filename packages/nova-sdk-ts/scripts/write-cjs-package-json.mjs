/**
 * Write `dist/cjs/package.json` after the CommonJS build (ADR-0194 D5).
 *
 * The root package.json declares `"type": "module"`, which Node applies to
 * every .js file beneath it. Without this marker Node would load the
 * CommonJS output as ESM and fail on `exports`/`require`. A nested
 * package.json scopes the override to `dist/cjs` only — the standard
 * dual-package layout, and the reason no bundler is needed here.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const target = join(here, "..", "dist", "cjs");

mkdirSync(target, { recursive: true });
writeFileSync(
  join(target, "package.json"),
  `${JSON.stringify({ type: "commonjs" }, null, 2)}\n`,
  "utf8",
);

console.log(`[sdk] wrote ${join(target, "package.json")} ({"type":"commonjs"})`);
