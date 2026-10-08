#!/usr/bin/env node
// Copy the built DASHBOARD — and only the dashboard — into
// src/novafabric/serve/static/, which the Python wheel packages and
// `nova serve --experimental` mounts at `/`.
//
// ADR-0299 decision 3: the wheel ships dashboard assets only. The public
// pages (/, /concepts, /install, /why, /spec, /showcase/*, /docs/*) belong to
// https://novafabric.ai; the dashboard links there as plain external links,
// so nothing in local mode requires internet access to operate.
//
// What lands in the target:
//   dashboard/      the client-only app shell (dist/dashboard/)
//   _astro/         ONLY the files the dashboard shell reaches, transitively —
//                   JS chunks, CSS, fonts, images (computed below, not a glob)
//   favicon.svg     referenced by the shell
//   index.html      product-local: forwards `/` to `/dashboard/`, keeping ?token=
//   404.html        product-local not-found page (no marketing navigation)
//
// Sibling directories owned by other build steps (e.g. topology/) are left
// untouched. Guarded by
// tests/packaging_metadata/test_serve_static_is_dashboard_only.py.

import {
  cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync,
} from 'node:fs';
import { basename, resolve } from 'node:path';

const ROOT = resolve(new URL('..', import.meta.url).pathname);
const REPO_ROOT = resolve(ROOT, '..');
const DIST = resolve(ROOT, 'dist');
const SHELL = resolve(ROOT, 'serve-shell');
const TARGET = resolve(REPO_ROOT, 'src/novafabric/serve/static');

// Every entry this script has ever written. All are removed before copying so
// a rebuild over an old checkout cannot leave a retired marketing page behind.
const MANAGED_ENTRIES = [
  '_astro', 'dashboard', 'favicon.svg', 'index.html', '404.html',
  // Retired by ADR-0299 — marketing routes now live on https://novafabric.ai.
  'concepts', 'install', 'why', 'spec', 'showcase', 'docs',
  'robots.txt', 'llms.txt', 'sitemap-index.xml', 'sitemap-0.xml',
];

const fail = (msg) => {
  console.error(`[error] ${msg}`);
  process.exit(1);
};

const shellHtml = resolve(DIST, 'dashboard', 'index.html');
if (!existsSync(shellHtml)) fail(`${shellHtml} not found — run 'astro build' first.`);
for (const page of ['index.html', '404.html']) {
  if (!existsSync(resolve(SHELL, page))) fail(`${resolve(SHELL, page)} is missing.`);
}

// --- transitive closure of _astro assets reachable from the shell ----------
// Vite emits content-hashed names and references them by literal string
// (static imports, dynamic imports, the preload map, CSS url()). Scanning every
// reached text file for any token that names an existing _astro file therefore
// finds the full closure; a coincidental match can only over-include.
const ASTRO = resolve(DIST, '_astro');
const available = new Set(existsSync(ASTRO) ? readdirSync(ASTRO) : []);
const ASSET_TOKEN = /[A-Za-z0-9_@~.-]+\.(?:m?js|css|woff2?|ttf|otf|svg|png|jpe?g|webp|avif|gif|ico)\b/g;
const SCANNABLE = /\.(?:html|m?js|css|svg)$/;

const needed = new Set();
const queue = [shellHtml, resolve(SHELL, 'index.html'), resolve(SHELL, '404.html')];
while (queue.length > 0) {
  const file = queue.shift();
  const text = readFileSync(file, 'utf8');
  for (const token of text.match(ASSET_TOKEN) ?? []) {
    const name = basename(token);
    if (!available.has(name) || needed.has(name)) continue;
    needed.add(name);
    if (SCANNABLE.test(name)) queue.push(resolve(ASTRO, name));
  }
}

// Every /_astro/ URL the shell names must resolve, or the dashboard is broken.
const shellRefs = readFileSync(shellHtml, 'utf8').match(/\/_astro\/[^"'\s)]+/g) ?? [];
const missing = shellRefs.map((r) => basename(r)).filter((n) => !needed.has(n));
if (missing.length > 0) fail(`dashboard shell references assets not in dist/_astro: ${missing.join(', ')}`);

// --- write the target ------------------------------------------------------
mkdirSync(TARGET, { recursive: true });
for (const entry of MANAGED_ENTRIES) {
  rmSync(resolve(TARGET, entry), { recursive: true, force: true });
}

cpSync(resolve(DIST, 'dashboard'), resolve(TARGET, 'dashboard'), { recursive: true });
mkdirSync(resolve(TARGET, '_astro'), { recursive: true });
let bytes = 0;
for (const name of [...needed].sort()) {
  cpSync(resolve(ASTRO, name), resolve(TARGET, '_astro', name));
  bytes += statSync(resolve(ASTRO, name)).size;
}
if (existsSync(resolve(DIST, 'favicon.svg'))) {
  cpSync(resolve(DIST, 'favicon.svg'), resolve(TARGET, 'favicon.svg'));
}
cpSync(resolve(SHELL, 'index.html'), resolve(TARGET, 'index.html'));
cpSync(resolve(SHELL, '404.html'), resolve(TARGET, '404.html'));

console.log(
  `[ok] dashboard copied → ${TARGET} ` +
  `(${needed.size} of ${available.size} _astro files, ${(bytes / 1024).toFixed(0)} KiB; ` +
  'no marketing pages — ADR-0299)',
);
