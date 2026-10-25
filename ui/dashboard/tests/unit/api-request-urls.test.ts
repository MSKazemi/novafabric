/**
 * Reviews frontend-dashboard-06, -07 and -16 — the shape of the URLs `request()` builds.
 *
 * - 06: the safety-case path carried its own `?template=…`, `request()` appended a
 *   second `?token=…`, the server read the token as part of `template`, answered 401
 *   and the dashboard signed the user out.
 * - 07: an `undefined` query value became the string "undefined" (the Cost tab's
 *   default view asked for run `undefined` and showed $0).
 * - 16: a file name with `#` or `?` was not encoded and addressed another resource.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api, setConnection } from '@/lib/api';

let fetched: string[] = [];

beforeEach(() => {
  fetched = [];
  setConnection('tok', 'http://127.0.0.1:4321');
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      fetched.push(url);
      return new Response(JSON.stringify({ ok: true, filename: 'f', content: '' }), { status: 200 });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('request() URL construction', () => {
  it('sends the safety-case template and the token as separate query parameters', async () => {
    await api.getRunSafetyCase('run-1', 'clymer-generic-v0');
    const url = new URL(fetched[0]);
    expect(fetched[0].match(/\?/g)).toHaveLength(1);
    expect(url.searchParams.get('template')).toBe('clymer-generic-v0');
    expect(url.searchParams.get('token')).toBe('tok');
  });

  it('drops undefined and null query values instead of sending "undefined"', async () => {
    await api.costReport({ run_id: undefined, days: 7 });
    const url = new URL(fetched[0]);
    expect(url.searchParams.has('run_id')).toBe(false);
    expect(fetched[0]).not.toContain('undefined');
    expect(url.searchParams.get('days')).toBe('7');
  });

  it('encodes each segment of a run file path', async () => {
    await api.getRunFile('run-1', 'sub dir/a#b?c.json');
    expect(fetched[0]).toContain('/api/runs/run-1/file/sub%20dir/a%23b%3Fc.json?token=tok');
    const url = new URL(fetched[0]);
    expect(url.pathname).toBe('/api/runs/run-1/file/sub%20dir/a%23b%3Fc.json');
  });
});
