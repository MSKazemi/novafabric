/**
 * ADR-0232 D4 — the Runs saved-views bar writes `nova view`s when the server
 * offers them, and falls back to browser-local views when it does not.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const listSavedViews = vi.fn();
const saveView = vi.fn();
const deleteView = vi.fn();

vi.mock('@/lib/api', () => ({
  api: {
    listSavedViews: (...a: unknown[]) => listSavedViews(...a),
    saveView: (...a: unknown[]) => saveView(...a),
    deleteView: (...a: unknown[]) => deleteView(...a),
  },
}));

import RunsSavedViewsBar from '@/components/dashboard/tabs/runs/RunsSavedViewsBar';

const current = {
  search: '', statusFilter: 'error' as const, sort: 'oldest' as const,
  since: '2026-09-01', until: '', filter: 'model:gpt-4o', scope: 'tree',
};

const serverView = (over: Record<string, unknown> = {}) => ({
  view_id: 'errs', name: 'errs', description: null, tags: ['dashboard:runs'],
  query: {}, created_at: '', updated_at: null, view_hash: 'sha256:x',
  dashboard: { f: 'status:error', scope: 'node', since: '', until: '', status: 'all', sort: 'newest' },
  dashboard_unavailable_reason: null, cli_equivalent: 'nova view run errs',
  ...over,
});

describe('RunsSavedViewsBar', () => {
  beforeEach(() => {
    localStorage.clear();
    listSavedViews.mockReset(); saveView.mockReset(); deleteView.mockReset();
  });

  it('saves the view state as a nova view and lists it back', async () => {
    listSavedViews.mockResolvedValueOnce({ views_dir: '/v', views: [], warnings: [] })
      .mockResolvedValueOnce({ views_dir: '/v', views: [serverView()], warnings: [] });
    saveView.mockResolvedValue({ ok: true, path: '/v/errs.yaml', view: serverView() });
    render(<RunsSavedViewsBar current={current} onApply={() => {}} />);
    await waitFor(() => expect(listSavedViews).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('Saved view name'), { target: { value: 'errs' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(saveView).toHaveBeenCalledWith('errs', {
      f: 'model:gpt-4o', scope: 'tree', since: '2026-09-01', until: '', status: 'error', sort: 'oldest',
    }, false));
    expect(await screen.findByTestId('server-view')).toHaveTextContent('errs');
    expect(localStorage.getItem('nova.savedViews.runs')).toBeNull();
  });

  it('applies a server view and disables one the Runs view cannot express', async () => {
    listSavedViews.mockResolvedValue({
      views_dir: '/v', warnings: [],
      views: [serverView(), serverView({
        view_id: 'two', name: 'two', dashboard: null,
        dashboard_unavailable_reason: "predicate 'model IN (a, b)' has no filter-bar form",
      })],
    });
    const onApply = vi.fn();
    render(<RunsSavedViewsBar current={current} onApply={onApply} />);
    fireEvent.click(await screen.findByRole('button', { name: 'errs' }));
    expect(onApply).toHaveBeenCalledWith(expect.objectContaining({ filter: 'status:error', scope: 'node', search: '' }));
    const two = screen.getByRole('button', { name: 'two' });
    expect(two).toBeDisabled();
    expect(two).toHaveAttribute('title', expect.stringContaining('no filter-bar form'));
  });

  it('falls back to browser-local views when the server has none to offer', async () => {
    listSavedViews.mockRejectedValue(new Error('404 Not Found'));
    render(<RunsSavedViewsBar current={current} onApply={() => {}} />);
    expect(await screen.findByTestId('views-local-fallback')).toHaveTextContent('404 Not Found');
    fireEvent.change(screen.getByLabelText('Saved view name'), { target: { value: 'mine' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(saveView).not.toHaveBeenCalled();
    expect(JSON.parse(localStorage.getItem('nova.savedViews.runs') ?? '[]')[0].name).toBe('mine');
    expect(screen.getByTestId('local-view')).toHaveTextContent('mine');
  });
});
