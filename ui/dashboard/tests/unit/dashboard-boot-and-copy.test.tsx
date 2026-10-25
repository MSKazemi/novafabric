/**
 * Reviews frontend-dashboard-18 and -19.
 *
 * - 18: any boot failure (server briefly down, network error) deleted the stored token
 *   and said "Token rejected"; only a 401 means the token is bad.
 * - 19: the manual classifier read fields the server does not send; the "not checked"
 *   verdict (`timestamp_ok: null`) was drawn as FAIL; the Admin tab said token values are
 *   stored on disk (only a digest is); `until` excluded the whole selected day.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { endOfDay } from '@/components/dashboard/tabs/runs/useRunSearch';

const TOKEN_KEY = 'novafabric.serve-token';

describe('boot', () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem(TOKEN_KEY, 'good-token');
    localStorage.setItem('novafabric.serve-base', 'http://127.0.0.1:4321');
  });
  afterEach(() => vi.unstubAllGlobals());

  async function boot() {
    const { default: DashboardApp } = await import('@/components/dashboard/DashboardApp');
    render(<DashboardApp />);
  }

  it('keeps the token when the server cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    await boot();
    await waitFor(() => expect(screen.getByText(/Could not reach the server/)).toBeTruthy());
    expect(localStorage.getItem(TOKEN_KEY)).toBe('good-token');
    expect(screen.queryByText(/Token rejected/)).toBeNull();
  });

  it('keeps the token on a 5xx', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 503, statusText: 'Unavailable' })));
    await boot();
    await waitFor(() => expect(screen.getByText(/Could not reach the server/)).toBeTruthy());
    expect(localStorage.getItem(TOKEN_KEY)).toBe('good-token');
  });

  it('clears the token only when the server answers 401', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 401 })));
    await boot();
    await waitFor(() => expect(screen.getByText(/Token rejected/)).toBeTruthy());
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
  });
});

describe('endOfDay', () => {
  it('turns a bare date into the end of that day so the selected day is included', () => {
    expect(endOfDay('2026-10-01')).toBe('2026-10-01T23:59:59.999999');
    expect('2026-10-01T12:00:00+00:00' <= endOfDay('2026-10-01')!).toBe(true);
  });
  it('leaves a full timestamp and an empty value alone', () => {
    expect(endOfDay('2026-10-01T05:00:00Z')).toBe('2026-10-01T05:00:00Z');
    expect(endOfDay('')).toBeUndefined();
  });
});
