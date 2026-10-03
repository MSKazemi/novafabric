import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.resolve(HERE, '..', 'dist');
const REPO_ROOT = path.resolve(HERE, '..', '..');
const ORIGIN = 'https://novafabric.ai';
const GITHUB_ORIGIN = 'https://github.com';
const GITHUB_BLOB_PREFIX = '/MSKazemi/novafabric/blob/main/';
const GITHUB_TREE_PREFIX = '/MSKazemi/novafabric/tree/main/';

/**
 * Pages served by the Next.js build under the same domain (MSKazemi/novafabric-web). They are
 * not in this build's dist/ by design, so they cannot be verified here; the deploy workflow
 * that merges both builds checks every link on the merged site, where they do exist.
 */
const NEXT_BUILD_PAGES = new Set([
  '/novafabric/',
  '/demo/',
  '/blog/',
  '/research/',
  '/primitives/',
  '/architecture/',
  '/changelog/',
  '/capsules/',
  '/contact/',
]);

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
let checkedGitHubTargets = 0;
let checkedOtherBuild = 0;

for (const file of htmlFiles) {
  // Astro emits a flat 404.html whose canonical href is /404/. That canonical
  // deliberately describes the error document, not a normal route that should
  // resolve to dist/404/index.html. Exclude the error document itself while
  // keeping every ordinary generated page under the same strict link gate.
  if (pageUrl(file) === '/404.html') continue;

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
    if (resolved.origin !== ORIGIN) {
      if (resolved.origin === GITHUB_ORIGIN) {
        const decoded = decodeURIComponent(resolved.pathname);
        const isBlob = decoded.startsWith(GITHUB_BLOB_PREFIX);
        const isTree = decoded.startsWith(GITHUB_TREE_PREFIX);
        if (isBlob || isTree) {
          const prefix = isBlob ? GITHUB_BLOB_PREFIX : GITHUB_TREE_PREFIX;
          const repoRelative = decoded.slice(prefix.length);
          const candidate = path.join(REPO_ROOT, repoRelative);
          checkedGitHubTargets += 1;
          if (
            !fs.existsSync(candidate) ||
            (isBlob && !fs.statSync(candidate).isFile()) ||
            (isTree && !fs.statSync(candidate).isDirectory())
          ) {
            failures.push(
              `${pageUrl(file)} -> ${href} (repository target missing or wrong type: ${repoRelative})`,
            );
          }
        }
      }
      continue;
    }

    const withSlash = resolved.pathname.endsWith('/') ? resolved.pathname : `${resolved.pathname}/`;
    if (NEXT_BUILD_PAGES.has(withSlash)) {
      checkedOtherBuild += 1;
      continue;
    }

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
  `Link check passed: ${checked} same-site links, ${checkedGitHubTargets} repository blob/tree targets and ${checkedOtherBuild} links to pages served by the other build, across ${htmlFiles.length} HTML pages.`,
);
