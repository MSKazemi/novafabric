import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.resolve(HERE, '..', 'dist');
const ORIGIN = 'https://novafabric.ai';

function walk(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}

function pageUrl(file) {
  const rel = path.relative(DIST, file).split(path.sep).join('/');
  if (rel === 'index.html') return '/';
  if (rel.endsWith('/index.html')) return '/' + rel.slice(0, -'index.html'.length);
  return '/' + rel;
}

function localCandidates(pathname) {
  const safe = pathname.replace(/^\/+/, '');
  const base = path.join(DIST, safe);
  if (pathname.endsWith('/')) return [path.join(base, 'index.html')];
  return [base, `${base}.html`, path.join(base, 'index.html')];
}

if (!fs.existsSync(DIST)) {
  console.error('dist/ does not exist; run the Astro build first.');
  process.exit(2);
}

const htmlFiles = walk(DIST).filter((file) => file.endsWith('.html'));
const failures = [];
let checked = 0;

for (const file of htmlFiles) {
  const html = fs.readFileSync(file, 'utf8');
  const base = new URL(pageUrl(file), ORIGIN);
  const re = /href\s*=\s*["']([^"'<>]+)["']/gi;
  let match;

  while ((match = re.exec(html)) !== null) {
    const href = match[1].trim();
    if (!href || href.startsWith('#')) continue;
    if (/^(?:mailto:|tel:|javascript:|data:)/i.test(href)) continue;

    let resolved;
    try {
      resolved = new URL(href, base);
    } catch {
      failures.push(`${pageUrl(file)} -> malformed href ${href}`);
      continue;
    }
    if (resolved.origin !== ORIGIN) continue;

    checked += 1;
    let pathname;
    try {
      pathname = decodeURIComponent(resolved.pathname);
    } catch {
      pathname = resolved.pathname;
    }

    const candidates = localCandidates(pathname);
    if (!candidates.some((candidate) => fs.existsSync(candidate))) {
      failures.push(
        `${pageUrl(file)} -> ${href} (expected one of ${candidates
          .map((candidate) => path.relative(DIST, candidate))
          .join(', ')})`,
      );
    }
  }
}

if (failures.length) {
  console.error(`Broken internal links: ${failures.length}`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}

console.log(
  `Internal link check passed: ${checked} links across ${htmlFiles.length} HTML pages.`,
);
