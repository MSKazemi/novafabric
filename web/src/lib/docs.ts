/**
 * Loads the repository's `docs/` markdown tree for publication at /docs/.
 *
 * The files are read from `../../../docs` — the same tree maintainers edit — so
 * the published site cannot drift from the repository. Nothing is copied.
 *
 * `import.meta.glob` with `eager: true` runs at build time, so this is a static
 * build with no runtime filesystem access.
 */

const modules = import.meta.glob<{
  compiledContent: () => string | Promise<string>;
  rawContent: () => string;
}>('../../../docs/**/*.md', { eager: true });

/** Files that are not user-facing documentation and should not be published. */
const EXCLUDE = [/^releases\//, /^whitepaper\//];

export interface DocPage {
  /** Path relative to docs/, e.g. "ops/monitoring.md". */
  file: string;
  /** URL slug, e.g. "ops/monitoring". */
  slug: string;
  html: string;
  raw: string;
}

function toFile(path: string): string {
  return path.replace(/^.*\/docs\//, '');
}

function toSlug(file: string): string {
  return file.replace(/\.md$/, '').replace(/(^|\/)README$/, '$1index').replace(/\/index$/, '');
}

const GITHUB_REPO = 'https://github.com/MSKazemi/novafabric';
const GITHUB_BLOB = `${GITHUB_REPO}/blob/main`;
const GITHUB_TREE = `${GITHUB_REPO}/tree/main`;

function splitTarget(href: string): { target: string; suffix: string } {
  const hashAt = href.indexOf('#');
  const beforeHash = hashAt >= 0 ? href.slice(0, hashAt) : href;
  const hash = hashAt >= 0 ? href.slice(hashAt) : '';
  const queryAt = beforeHash.indexOf('?');
  if (queryAt < 0) return { target: beforeHash, suffix: hash };
  return {
    target: beforeHash.slice(0, queryAt),
    suffix: beforeHash.slice(queryAt) + hash,
  };
}

/** Resolve a docs-relative target to a path in the repository root. */
function resolveRepoPath(file: string, target: string): string | null {
  const dir = file.includes('/') ? file.slice(0, file.lastIndexOf('/')) : '';
  const resolved = ['docs', ...dir.split('/').filter(Boolean)];

  for (const segment of target.split('/')) {
    if (!segment || segment === '.') continue;
    if (segment === '..') {
      if (!resolved.length) return null;
      resolved.pop();
      continue;
    }
    resolved.push(segment);
  }
  return resolved.join('/');
}

/**
 * Rewrite every repository-relative link according to what is actually
 * published on novafabric.ai.
 *
 * - a Markdown target whose slug is in `publishedSlugs` -> /docs/<slug>/
 * - the docs root -> /docs/
 * - every other repository file -> GitHub blob URL
 * - every other repository directory -> GitHub tree URL
 *
 * The published slug set is deliberately an input. Guessing publication from
 * the `.md` extension is what caused excluded docs and non-Markdown assets to
 * turn into site 404s.
 */
export function rewriteLinks(
  html: string,
  file: string,
  publishedSlugs: ReadonlySet<string>,
): string {
  return html.replace(/href="([^"]+)"/g, (whole, href: string) => {
    if (/^(?:[a-z][a-z0-9+.-]*:|\/|#|\?)/i.test(href)) return whole;

    const { target, suffix } = splitTarget(href);
    if (!target) return whole;

    const repoPath = resolveRepoPath(file, target);
    if (!repoPath) return whole;

    // A bare "." from a top-level docs page means the published docs root.
    if (repoPath === 'docs') return `href="/docs/${suffix}"`;

    if (target.toLowerCase().endsWith('.md') && repoPath.startsWith('docs/')) {
      const docFile = repoPath.slice('docs/'.length);
      const slug = toSlug(docFile);
      if (slug === '' || slug === 'index') return `href="/docs/${suffix}"`;
      if (publishedSlugs.has(slug)) {
        return `href="/docs/${slug}/${suffix}"`;
      }
    }

    const isDirectory = target.endsWith('/');
    const githubBase = isDirectory ? GITHUB_TREE : GITHUB_BLOB;
    return `href="${githubBase}/${repoPath}${suffix}"`;
  });
}

let cache: DocPage[] | null = null;

export async function docPages(): Promise<DocPage[]> {
  if (cache) return cache;

  // Pass 1 decides the exact public route set before any link is rewritten.
  // This is the source of truth for whether a relative Markdown target belongs
  // on novafabric.ai or should point back to the repository.
  const published = Object.entries(modules)
    .map(([path, mod]) => {
      const file = toFile(path);
      return { file, slug: toSlug(file), mod };
    })
    // The docs index itself is rendered by pages/docs/index.astro, and an empty
    // slug would collide with it.
    .filter((page) => page.slug !== '' && page.slug !== 'index')
    .filter((page) => !EXCLUDE.some((pattern) => pattern.test(page.file)));

  const publishedSlugs = new Set(published.map((page) => page.slug));

  // Pass 2 renders with the complete slug set available to rewriteLinks().
  // `compiledContent()` is async in Astro 5+; awaiting it keeps the build
  // static while allowing Astro's Markdown pipeline to finish first.
  const pages = await Promise.all(
    published.map(async ({ file, slug, mod }) => ({
      file,
      slug,
      html: rewriteLinks(await mod.compiledContent(), file, publishedSlugs),
      raw: mod.rawContent(),
    })),
  );

  cache = pages.sort((a, b) => a.slug.localeCompare(b.slug));
  return cache;
}

/** First `# heading`, falling back to a humanised slug. */
export function titleFor(page: DocPage): string {
  const heading = page.raw.match(/^#\s+(.+?)\s*$/m);
  if (heading) return heading[1].replace(/`/g, '');
  const last = page.slug.split('/').pop() ?? page.slug;
  return last.replace(/-/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

/**
 * First real prose paragraph, trimmed to a meta-description length.
 *
 * Skips the heading, blockquote callouts, badges, and code fences — a
 * description built from a badge row is worse than no description at all.
 */
export function descriptionFor(page: DocPage): string {
  const body = page.raw
    .replace(/^#\s+.+$/m, '')
    .replace(/```[\s\S]*?```/g, '')
    .replace(/^\s*[>|].*$/gm, '')
    .replace(/^\s*\[!\[.*$/gm, '');

  const paragraph = body
    .split(/\n\s*\n/)
    .map((block) => block.trim())
    .find((block) => block.length > 40 && !block.startsWith('#') && !block.startsWith('|'));

  if (!paragraph) return `${titleFor(page)} — NovaFabric documentation.`;

  const flat = paragraph
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[*_`]/g, '')
    .replace(/\s+/g, ' ')
    .trim();

  return flat.length > 155 ? `${flat.slice(0, 152).trimEnd()}…` : flat;
}
