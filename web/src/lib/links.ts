/**
 * External link constants. Centralised so a repo move or rename is a 1-line edit.
 *
 * The canonical GitHub repo path is `MSKazemi/novafabric` per the project README.
 * The repo is public, so every constant below must name a path the **public**
 * repository actually tracks — not merely a file that exists in a working tree.
 * `design/` is maintainer-private and publishes nothing, so a blob link into that
 * tree is a guaranteed 404 for every visitor. Guarded by
 * `tests/docs/test_site_links_resolve_publicly.py`.
 */

export const GITHUB_REPO = 'https://github.com/MSKazemi/novafabric';

export const githubBlob = (relativePath: string): string =>
  `${GITHUB_REPO}/blob/main/${relativePath.replace(/^\/+/, '')}`;

// ADR-0027 itself is private; the public decisions index records its number,
// title, status and date, which is what a reader following this link wants.
export const ADR_0027_URL = githubBlob('docs/decisions.md');
// The non-goals live privately in design/strategy/non-goals.md; the published
// statement of them is the architecture doc's "What NovaFabric is not".
export const NON_GOALS_URL = githubBlob('docs/architecture.md') + '#what-novafabric-is-not';
export const SCHEMAS_BASE_URL = githubBlob('schemas');
