#!/usr/bin/env node
// Copy the entire built site into src/novafabric/serve/static/ so the
// Python wheel can serve every page locally, not just the dashboard.
//
// Why the whole site (not just /dashboard/):
//   - The dashboard's header includes nav links to /concepts, /showcase/*,
//     /install, /why, /spec. Under `nova serve` those would 404 if we
//     shipped only the dashboard route.
//   - Showcase pages are tiny HTML files (the heavy chunks live in _astro/
//     and are already shared between targets), so the marginal size is small.
//   - Users browsing live data benefit from the explanatory pages right
//     there — same origin, no separate deploy required.

import { mkdirSync, cpSync, existsSync, rmSync } from 'node:fs';
import { resolve } from 'node:path';

const ROOT = resolve(new URL('..', import.meta.url).pathname);
const REPO_ROOT = resolve(ROOT, '..');
const DIST = resolve(ROOT, 'dist');
const TARGET = resolve(REPO_ROOT, 'src/novafabric/serve/static');

if (!existsSync(DIST)) {
  console.error(`[error] ${DIST} not found — run 'astro build' first.`);
  process.exit(1);
}

mkdirSync(TARGET, { recursive: true });

// Copy the entries the web build owns. Only delete+replace those entries so
// that sibling directories managed by other build steps (e.g. topology/) are
// preserved across rebuilds.
const OWNED_ENTRIES = [
  '_astro', 'favicon.svg', 'index.html', 'dashboard',
  'concepts', 'install', 'why', 'spec', 'showcase',
  '404.html', 'robots.txt',
];

for (const entry of OWNED_ENTRIES) {
  const src = resolve(DIST, entry);
  const dst = resolve(TARGET, entry);
  if (existsSync(src)) {
    if (existsSync(dst)) {
      rmSync(dst, { recursive: true, force: true });
    }
    cpSync(src, dst, { recursive: true });
  }
}

console.log(`[ok] full site copied → ${TARGET}`);
