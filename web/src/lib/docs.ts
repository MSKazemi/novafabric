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

const GITHUB_BLOB = 'https://github.com/novafabric/novafabric/blob/main';

/**
 * Rewrites the relative `.md` links the repository uses into URLs that work on
 * the web.
 *
 * Markdown links like `concepts.md` are correct in a git checkout and dead on a
 * site whose routes are `/docs/concepts/`. Links that escape `docs/` — the
 * README, CONTRIBUTING, the schemas directory — have no site route at all, so
 * they go to GitHub rather than nowhere.
 */
function rewriteLinks(html: string, file: string): string {
  const dir = file.includes('/') ? file.slice(0, file.lastIndexOf('/')) : '';

  return html.replace(/href="([^"]+)"/g, (whole, href: string) => {
    if (/^(?:[a-z]+:|\/|#)/i.test(href)) return whole;

    const [target, anchor = ''] = href.split('#');
    if (!target.endsWith('.md')) return whole;

    // Resolve the link relative to the current file's directory.
    const segments = (dir ? `${dir}/${target}` : target).split('/');
    const resolved: string[] = [];
    let escapes = 0;
    for (const segment of segments) {
      if (segment === '.' || segment === '') continue;
      if (segment === '..') {
        if (resolved.length) resolved.pop();
        else escapes += 1;
        continue;
      }
      resolved.push(segment);
    }

    const suffix = anchor ? `#${anchor}` : '';
    if (escapes > 0) {
      // Outside docs/ — no site route exists; send the reader to the source.
      return `href="${GITHUB_BLOB}/${resolved.join('/')}${suffix}"`;
    }
    return `href="/docs/${toSlug(resolved.join('/'))}/${suffix}"`;
  });
}

let cache: DocPage[] | null = null;

export async function docPages(): Promise<DocPage[]> {
  if (cache) return cache;

  // `compiledContent()` is async in Astro 5+; awaiting it here keeps every
  // caller synchronous-looking while still building statically.
  const pages = await Promise.all(
    Object.entries(modules).map(async ([path, mod]) => {
      const file = toFile(path);
      return {
        file,
        slug: toSlug(file),
        html: rewriteLinks(await mod.compiledContent(), file),
        raw: mod.rawContent(),
      };
    }),
  );

  cache = pages
    // The docs index itself is rendered by pages/docs/index.astro, and an empty
    // slug would collide with it.
    .filter((page) => page.slug !== '' && page.slug !== 'index')
    .filter((page) => !EXCLUDE.some((pattern) => pattern.test(page.file)))
    .sort((a, b) => a.slug.localeCompare(b.slug));

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
