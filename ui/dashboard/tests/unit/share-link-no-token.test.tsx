/**
 * Issue #278 — share and deep links never carry the dashboard token.
 *
 * Start-up removes `?token=` from the address bar, but that is one step that can
 * be skipped. Every URL the dashboard hands out (the run summary's "Copy link",
 * the evidence cart's `added_from`, which is written into an exported bundle)
 * is stripped of credentials on its own.
 */
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CapsuleSummary } from '@/components/dashboard/tabs/runs/ExplorerPanels';
import type { FullCapsule, RunSummary } from '@/lib/api';
import { useEvidenceCart } from '@/lib/evidenceCart';
import {
  shareableHash,
  shareableSearch,
  shareableUrl,
  shareableViewRef,
} from '@/lib/shareLink';

const SECRET = 'nf-secret-token-0123456789';
const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

describe('shareableUrl', () => {
  it('removes the token and keeps every other parameter and the anchor', () => {
    const out = shareableUrl(`http://127.0.0.1:4321/dashboard/?tab=runs&token=${SECRET}&run=R1#trace`);
    expect(out).not.toContain(SECRET);
    const url = new URL(out);
    expect(url.searchParams.get('token')).toBeNull();
    expect(url.searchParams.get('tab')).toBe('runs');
    expect(url.searchParams.get('run')).toBe('R1');
    expect(url.hash).toBe('#trace');
  });

  it('matches credential names case-insensitively and covers access_token', () => {
    const out = shareableUrl(`http://localhost:4321/dashboard/?Token=${SECRET}&ACCESS_TOKEN=${SECRET}&tab=diff`);
    expect(out).not.toContain(SECRET);
    expect(out).toBe('http://localhost:4321/dashboard/?tab=diff');
  });

  it('removes a token written into the fragment', () => {
    const out = shareableUrl(`http://127.0.0.1:4321/topology/#token=${SECRET}&view=tv5`);
    expect(out).not.toContain(SECRET);
    expect(out).toBe('http://127.0.0.1:4321/topology/#view=tv5');
  });

  it('drops user:password@ credentials too', () => {
    const out = shareableUrl(`http://user:${SECRET}@127.0.0.1:4321/dashboard/`);
    expect(out).toBe('http://127.0.0.1:4321/dashboard/');
  });

  it('leaves a URL without credentials byte for byte', () => {
    const href = 'http://127.0.0.1:4321/dashboard/?tab=runs&f=status%3Aerror#tv5';
    expect(shareableUrl(href)).toBe(href);
  });

  it('gives no link at all for an unparseable href', () => {
    expect(shareableUrl(`not a url ?token=${SECRET}`)).toBe('');
  });
});

describe('shareableSearch / shareableHash / shareableViewRef', () => {
  it('returns an empty query when the token was the only parameter', () => {
    expect(shareableSearch(`?token=${SECRET}`)).toBe('');
    expect(shareableHash(`#token=${SECRET}`)).toBe('');
  });

  it('keeps a plain anchor', () => {
    expect(shareableHash('#tv5')).toBe('#tv5');
    expect(shareableHash('')).toBe('');
  });

  it('builds a relative view reference without the token', () => {
    expect(shareableViewRef('/dashboard/', `?tab=runs&token=${SECRET}`)).toBe('/dashboard/?tab=runs');
  });
});

describe('dashboard surfaces with ?token= still in the address bar', () => {
  beforeEach(() => {
    sessionStorage.clear();
    window.history.replaceState({}, '', `/dashboard/?tab=runs&token=${SECRET}`);
  });

  afterEach(() => {
    window.history.replaceState({}, '', '/');
    vi.restoreAllMocks();
    if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor);
    else Reflect.deleteProperty(navigator, 'clipboard');
  });

  it('records the evidence-cart view reference without the token', () => {
    const { result } = renderHook(() => useEvidenceCart());
    act(() => result.current.add('run', 'R1'));
    const raw = sessionStorage.getItem('nova.evidenceCart.v1') ?? '';
    expect(raw).not.toContain(SECRET);
    const stored = JSON.parse(raw);
    expect(stored[0].added_from).toBe('/dashboard/?tab=runs');
  });

  it('copies a run link without the token', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const run: RunSummary = {
      run_id: 'R1', status: 'ok', created_at: '2026-09-20T10:00:00Z', finished_at: null,
      duration_ms: 1, exit_code: 0, model_call_count: 0, tool_call_count: 0, mutating_tool_count: 0,
      command: ['python', 'agent.py'], novafabric_version: '0.102.1', capsule_path: '/c/R1',
    };
    const capsule = {
      run_id: 'R1', capsule_path: '/c/R1', manifest: { run_id: 'R1' }, trace: [],
      model_calls: [], tool_calls: [], lineage: [], assets: [], inputs: [], outputs: [],
    } as unknown as FullCapsule;
    render(<CapsuleSummary run={run} capsule={capsule} onAction={() => {}} />);

    const button = screen.getByRole('button', { name: /^Copy link / });
    expect(button.getAttribute('aria-label')).not.toContain(SECRET);
    fireEvent.click(button);
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = String((writeText.mock.calls[0] as unknown[])[0]);
    expect(copied).not.toContain(SECRET);
    expect(new URL(copied).searchParams.get('tab')).toBe('runs');
  });
});
