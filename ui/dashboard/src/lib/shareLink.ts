/**
 * Share and deep links never carry the dashboard token (issue #278).
 *
 * `nova serve` prints a `/dashboard?token=…` URL and the dashboard removes the
 * token from the address bar at start-up. That removal is one step that can be
 * skipped (blocked storage, an error before it ran, a URL typed by hand), so
 * every URL the dashboard hands to a person or writes into an artifact — the
 * run summary's "Copy link", the evidence cart's `added_from` — goes through
 * these helpers as well.
 */

/** Query/fragment parameter names that carry a credential (compared case-insensitively). */
export const CREDENTIAL_PARAMS: readonly string[] = ['token', 'access_token'];

function isCredentialParam(name: string): boolean {
  return CREDENTIAL_PARAMS.includes(name.toLowerCase());
}

/** A copy of `params` without any credential parameter. */
export function withoutCredentialParams(params: URLSearchParams): URLSearchParams {
  const out = new URLSearchParams();
  params.forEach((v, k) => {
    if (!isCredentialParam(k)) out.append(k, v);
  });
  return out;
}

function hasCredentialParam(params: URLSearchParams): boolean {
  let found = false;
  params.forEach((_v, k) => { if (isCredentialParam(k)) found = true; });
  return found;
}

/**
 * `search` (with or without its leading `?`) minus credentials, as `?a=b` or `''`.
 * A query without a credential is returned byte for byte (no re-encoding).
 */
export function shareableSearch(search: string): string {
  const body = search.replace(/^\?/, '');
  const params = new URLSearchParams(body);
  if (!hasCredentialParam(params)) return body ? `?${body}` : '';
  const qs = withoutCredentialParams(params).toString();
  return qs ? `?${qs}` : '';
}

/**
 * A fragment minus credentials. A plain anchor (`#tv5`) is kept as is; a
 * fragment written as parameters (`#token=…&x=1`) loses its credential ones.
 */
export function shareableHash(hash: string): string {
  const body = hash.replace(/^#/, '');
  if (!body.includes('=')) return body ? `#${body}` : '';
  const params = new URLSearchParams(body);
  if (!hasCredentialParam(params)) return `#${body}`;
  const qs = withoutCredentialParams(params).toString();
  return qs ? `#${qs}` : '';
}

/**
 * `href` with every credential parameter removed from its query and fragment
 * (and any `user:password@`, which `URL.origin` leaves out). An unparseable href
 * gives `''` — no link rather than one that might hold a token.
 */
export function shareableUrl(href: string): string {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return '';
  }
  return `${url.origin}${url.pathname}${shareableSearch(url.search)}${shareableHash(url.hash)}`;
}

/** A relative view reference (`/dashboard/?tab=runs`) minus credentials. */
export function shareableViewRef(pathname: string, search: string): string {
  return `${pathname}${shareableSearch(search)}`;
}
