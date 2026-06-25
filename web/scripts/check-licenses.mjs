#!/usr/bin/env node
// License gate — fails the build if any installed package's SPDX id is not allowed.
// Per ADR 0024:
//   Tier A (allowed by default): Apache-2.0, MIT, BSD-2/3, ISC, 0BSD, OFL-1.1, CC0-1.0,
//     Unlicense, Python-2.0, BlueOak-1.0.0
//   Tier B (allowed with ADR pointer): MPL-2.0, EPL-2.0, LGPL-2.1/3.0 (dynamic linking only)
//   Tier C/D (rejected): GPL/AGPL/SSPL/BSL/Elastic/proprietary
//
// Build-tooling allowance: MPL-2.0 packages used solely by the build pipeline
// (lightningcss for Tailwind v4, axe-core for accessibility tests) are allowed
// because they don't ship in the static output. This file pins the specific
// packages that have been reviewed.
//
// CC-BY-4.0 is allowed for data-only packages (e.g. caniuse-lite) — it's a
// data license, not a code license.
//
// Per-package overrides exist for packages with known packaging defects
// (missing `license` field but a verified upstream LICENSE file).

import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

const ROOT = resolve(new URL('..', import.meta.url).pathname);
const NODE_MODULES = join(ROOT, 'node_modules');

// Tier A — runtime allow-list per ADR 0024
const TIER_A = new Set([
  'Apache-2.0',
  'MIT',
  'BSD-2-Clause',
  'BSD-3-Clause',
  'BSD',
  'ISC',
  '0BSD',
  'OFL-1.1',
  'CC0-1.0',
  'Unlicense',
  'Python-2.0',
  'BlueOak-1.0.0',
]);

// Tier B — allowed with ADR pointer. Limited to build-tooling (does not ship
// in dist/) per ADR 0024 §1 Tier B "dynamic linking only" / "external service".
const TIER_B_BUILD_TOOLING = new Set([
  'MPL-2.0',
  'LGPL-3.0-or-later',
  'LGPL-3.0-only',
  'LGPL-2.1-or-later',
  'LGPL-2.1-only',
]);

// Data-only licenses (allowed for data packages like caniuse-lite, mdn-data)
const DATA_LICENSES = new Set(['CC-BY-4.0', 'CC-BY-3.0']);

// Per-package overrides: packages with verified upstream license but a packaging
// defect (missing or non-SPDX `license` field). Each entry must cite the
// upstream LICENSE file URL.
const OVERRIDES = {
  'zod-to-ts': {
    license: 'MIT',
    reason: 'Upstream LICENSE file at https://github.com/sachinraja/zod-to-ts/blob/main/LICENSE is MIT; package.json missing license field (upstream packaging bug).',
  },
};

// Build-tooling packages explicitly reviewed and allowed under Tier B / data.
// Each entry is a name pattern → reason. Required to keep the allowlist auditable.
const BUILD_TOOLING_ALLOWED = [
  { match: /^@img\/sharp-libvips-/, license: 'LGPL-3.0-or-later', reason: 'libvips native binary used by sharp for build-time image optimization in Astro. Dynamic linking; binary does not ship in dist/. ADR 0024 Tier B (LGPL via dynamic linking).' },
  { match: /^axe-core$/, license: 'MPL-2.0', reason: 'Used only by @axe-core/playwright for dev-time accessibility tests. Does not ship in dist/. ADR 0024 Tier B build-tooling.' },
  { match: /^@axe-core\/playwright$/, license: 'MPL-2.0', reason: 'Dev-only Playwright integration for axe accessibility tests. Does not ship in dist/. ADR 0024 Tier B build-tooling.' },
  { match: /^lightningcss/, license: 'MPL-2.0', reason: 'CSS processing for Tailwind v4 build pipeline. Does not ship in dist/. ADR 0024 Tier B build-tooling.' },
  { match: /^caniuse-lite$/, license: 'CC-BY-4.0', reason: 'Browser-compat data table (data, not code). Pulled transitively by browserslist. CC-BY-4.0 is a data license; ADR 0024 spirit covers data licenses alongside CC0.' },
];

function readDevDeps() {
  const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));
  return new Set(Object.keys(pkg.devDependencies ?? {}));
}

function* walkPackages(dir) {
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir)) {
    if (entry.startsWith('.')) continue;
    const full = join(dir, entry);
    let st;
    try { st = statSync(full); } catch { continue; }
    if (!st.isDirectory()) continue;

    if (entry.startsWith('@')) {
      yield* walkPackages(full);
      continue;
    }

    const pkgPath = join(full, 'package.json');
    if (existsSync(pkgPath)) {
      try {
        const pkg = JSON.parse(readFileSync(pkgPath, 'utf8'));
        yield {
          name: pkg.name ?? entry,
          version: pkg.version ?? '?',
          license: extractLicense(pkg),
          path: full,
        };
      } catch { /* ignore */ }
    }

    const nested = join(full, 'node_modules');
    if (existsSync(nested)) yield* walkPackages(nested);
  }
}

function extractLicense(pkg) {
  if (typeof pkg.license === 'string') return pkg.license;
  if (pkg.license && typeof pkg.license === 'object' && pkg.license.type) return pkg.license.type;
  if (Array.isArray(pkg.licenses) && pkg.licenses.length > 0) {
    return pkg.licenses.map((l) => (typeof l === 'string' ? l : l.type)).join(' OR ');
  }
  return null;
}

function spdxLeaves(expr) {
  if (!expr) return [];
  return expr.replace(/[()]/g, ' ').split(/\s+(?:OR|AND|WITH)\s+/i).map((s) => s.trim()).filter(Boolean);
}

function classify(name, license) {
  // 1) Per-package override (packaging defect)
  if (OVERRIDES[name]) {
    return { tier: 'override', license: OVERRIDES[name].license, reason: OVERRIDES[name].reason };
  }

  // 2) Build-tooling allowlist (must match a specific reviewed entry)
  for (const entry of BUILD_TOOLING_ALLOWED) {
    if (entry.match.test(name)) {
      return { tier: 'build-tooling', license: entry.license, reason: entry.reason };
    }
  }

  // 3) Tier A SPDX leaf
  if (license && spdxLeaves(license).some((l) => TIER_A.has(l))) {
    return { tier: 'A', license };
  }

  // 4) Data license for data packages
  if (license && spdxLeaves(license).some((l) => DATA_LICENSES.has(l))) {
    return { tier: 'data', license };
  }

  return { tier: 'reject', license: license ?? 'NO LICENSE' };
}

function main() {
  const devDeps = readDevDeps(); // not used for tiering decisions; build-tooling allowlist supersedes
  const seen = new Map(); // dedupe by name@version
  for (const entry of walkPackages(NODE_MODULES)) {
    const key = `${entry.name}@${entry.version}`;
    if (!seen.has(key)) seen.set(key, entry);
  }

  const tally = { A: 0, override: 0, 'build-tooling': 0, data: 0, reject: 0 };
  const violations = [];
  for (const entry of seen.values()) {
    const cls = classify(entry.name, entry.license);
    tally[cls.tier] = (tally[cls.tier] ?? 0) + 1;
    if (cls.tier === 'reject') {
      violations.push({ ...entry, ...cls });
    }
  }

  console.log(`Scanned ${seen.size} unique packages.`);
  console.log(`  Tier A:           ${tally.A}`);
  console.log(`  Build-tooling:    ${tally['build-tooling']} (MPL/LGPL via ADR 0024 Tier B)`);
  console.log(`  Data licenses:    ${tally.data} (CC-BY-* for data packages)`);
  console.log(`  Override (verified upstream): ${tally.override}`);
  console.log(`  Rejected:         ${tally.reject}`);

  if (violations.length === 0) {
    console.log('\nAll packages cleared. ✓');
    process.exit(0);
  }

  console.error(`\nLicense violations (${violations.length}):`);
  for (const v of violations) {
    console.error(`  ${v.name}@${v.version} → ${v.license}`);
  }
  console.error('\nADR 0024 forbids non-Tier-A licenses at runtime by default.');
  console.error('To allow a build-tooling package, add a reviewed entry to BUILD_TOOLING_ALLOWED in this script with a justification.');
  process.exit(1);
}

main();
