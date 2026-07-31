/**
 * Pins useUrlState behavior before the modernization builds sub-navigation
 * (?sub=) on top of it.
 */
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { useUrlState } from '@/lib/useUrlState';

describe('useUrlState', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/dashboard');
  });

  it('returns the default when the param is absent', () => {
    const { result } = renderHook(() => useUrlState('sub', 'frameworks'));
    expect(result.current[0]).toBe('frameworks');
    expect(window.location.search).toBe('');
  });

  it('reads an existing param from the URL', () => {
    window.history.replaceState({}, '', '/dashboard?sub=privacy');
    const { result } = renderHook(() => useUrlState('sub', 'frameworks'));
    expect(result.current[0]).toBe('privacy');
  });

  it('writes non-default values to the URL and omits the default', () => {
    const { result } = renderHook(() => useUrlState('sub', 'frameworks'));
    act(() => result.current[1]('privacy'));
    expect(new URLSearchParams(window.location.search).get('sub')).toBe('privacy');
    act(() => result.current[1]('frameworks'));
    expect(new URLSearchParams(window.location.search).get('sub')).toBeNull();
  });

  it('preserves other params when writing', () => {
    window.history.replaceState({}, '', '/dashboard?tab=compliance');
    const { result } = renderHook(() => useUrlState('sub', ''));
    act(() => result.current[1]('audits'));
    const params = new URLSearchParams(window.location.search);
    expect(params.get('tab')).toBe('compliance');
    expect(params.get('sub')).toBe('audits');
  });

  it('reflects popstate (back/forward) into state', () => {
    const { result } = renderHook(() => useUrlState('sub', 'frameworks'));
    act(() => {
      window.history.replaceState({}, '', '/dashboard?sub=privacy');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    expect(result.current[0]).toBe('privacy');
  });
});
