/**
 * External link constants. Centralised so a repo move or rename is a 1-line edit.
 *
 * Note: the canonical GitHub repo path is `MSKazemi/novafabric` per the project
 * README. Until the repo is public, anonymous fetches will 404 — that's expected.
 */

export const GITHUB_REPO = 'https://github.com/MSKazemi/novafabric';

export const githubBlob = (relativePath: string): string =>
  `${GITHUB_REPO}/blob/main/${relativePath.replace(/^\/+/, '')}`;

export const ADR_0027_URL = githubBlob('design/adr/0027-nova-serve-experimental-dashboard.md');
export const NON_GOALS_URL = githubBlob('design/strategy/non-goals.md');
export const SCHEMAS_BASE_URL = githubBlob('schemas');
