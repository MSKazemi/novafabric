/**
 * Dashboard editor (validate-then-save) — acceptance criteria pinned here:
 *
 * - the server's verdict is shown, a refusal with its own reason, and the UI
 *   never decides validity itself (an obviously bad document is still sent);
 * - Save is unavailable until the server accepted the document *as typed*, and
 *   any later edit withdraws that acceptance;
 * - the preview is a diff against the stored bytes (or the whole new file) and
 *   a no-op (`unchanged`) offers no Save;
 * - Save sends the reviewed digest (`base_sha256`); a 409 / 403 / 422 from the
 *   server is shown verbatim, nothing is claimed as saved;
 * - after a write the lists refresh; the guided form composes JSON text only;
 * - keyboard: tabs are a roving tablist, Ctrl+Enter validates, Esc closes.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '@/lib/ToastContext';
import type { DashboardValidateResponse } from '@/lib/dashboardTypes';
import { EMPTY_WIDGET_FIELDS, composeWidgetText, lineDiff } from '@/components/dashboard/dashboards/editorModel';

const mocks = vi.hoisted(() => {
  class ServeApiError extends Error {
    constructor(public status: number, message: string) {
      super(message);
      this.name = 'ServeApiError';
    }
  }
  return {
    ServeApiError,
    validateDashboardDocument: vi.fn(),
    applyDashboardDocument: vi.fn(),
    listDashboards: vi.fn(),
    getDashboard: vi.fn(),
    getWidgetData: vi.fn(),
    exportDashboardDocument: vi.fn(),
  };
});

vi.mock('@/lib/api', () => ({
  ServeApiError: mocks.ServeApiError,
  api: {
    validateDashboardDocument: mocks.validateDashboardDocument,
    applyDashboardDocument: mocks.applyDashboardDocument,
    listDashboards: mocks.listDashboards,
    getDashboard: mocks.getDashboard,
    getWidgetData: mocks.getWidgetData,
    exportDashboardDocument: mocks.exportDashboardDocument,
  },
}));

import DashboardEditor from '@/components/dashboard/dashboards/DashboardEditor';
import DashboardsView from '@/components/dashboard/dashboards/DashboardsView';

const OLD = '{\n  "id": "w1",\n  "title": "Old"\n}\n';
const NEW = '{\n  "id": "w1",\n  "title": "New"\n}\n';

function accepted(over: Partial<DashboardValidateResponse> = {}): DashboardValidateResponse {
  return {
    ok: true,
    kind: 'widget',
    id: 'w1',
    title: 'New',
    action: 'update',
    normalized: NEW,
    existing: OLD,
    proposed_sha256: 'p'.repeat(64),
    current_sha256: 'c'.repeat(64),
    warnings: [],
    cli_equivalent: 'nova dashboard show w1',
    ...over,
  };
}

function renderEditor(props: Partial<Parameters<typeof DashboardEditor>[0]> = {}) {
  const onSaved = vi.fn();
  const onClose = vi.fn();
  render(
    <ToastProvider>
      <DashboardEditor onSaved={onSaved} onClose={onClose} {...props} />
    </ToastProvider>,
  );
  return { onSaved, onClose };
}

const box = () => screen.getByRole('textbox', { name: /Widget or dashboard JSON/ });

beforeEach(() => {
  for (const m of [mocks.validateDashboardDocument, mocks.applyDashboardDocument]) m.mockReset();
});

describe('editor model', () => {
  it('diffs against the stored bytes, and shows a new file as all additions', () => {
    expect(lineDiff(OLD, NEW).map((l) => `${l.kind}:${l.text.trim()}`)).toEqual([
      'same:{',
      'same:"id": "w1",',
      'del:"title": "Old"',
      'add:"title": "New"',
      'same:}',
    ]);
    expect(lineDiff(null, NEW).every((l) => l.kind === 'add')).toBe(true);
    expect(lineDiff(NEW, NEW).every((l) => l.kind === 'same')).toBe(true);
  });

  it('composes guided fields into text and only checks that the query is JSON', () => {
    const ok = composeWidgetText({ ...EMPTY_WIDGET_FIELDS, id: 'A b', title: 't', unit: 'USD', stacked: true });
    expect('text' in ok && JSON.parse(ok.text)).toMatchObject({
      $novafabricWidget: true,
      id: 'A b', // not judged here — the server decides what an id may be
      presentation: { chart: 'bar', unit: 'USD', stacked: true },
      query: { select: ['count()'] },
    });
    expect(composeWidgetText({ ...EMPTY_WIDGET_FIELDS, query: '{nope' })).toHaveProperty('error');
  });
});

describe('DashboardEditor', () => {
  it('shows the server refusal with its reason and offers no Save', async () => {
    mocks.validateDashboardDocument.mockResolvedValue({
      ok: false,
      error: "widget 'x' carries a query the DSL rejects: unknown column",
      warnings: [],
    });
    renderEditor();
    await userEvent.type(box(), '{{"obviously": "wrong"}');
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    // The UI did not pre-judge: the (bad) document was sent to the server.
    expect(mocks.validateDashboardDocument).toHaveBeenCalledWith({ text: '{"obviously": "wrong"}' });
    const refusal = await screen.findByTestId('refusal');
    expect(refusal).toHaveTextContent('the DSL rejects: unknown column');
    expect(refusal).toHaveTextContent(/nothing written/);
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('shows a diff for an update, saves with the reviewed digest, and reports the server answer', async () => {
    mocks.validateDashboardDocument.mockResolvedValue(accepted());
    mocks.applyDashboardDocument.mockResolvedValue({
      ok: true, kind: 'widget', id: 'w1', file: 'w1.widget.json', changed: true,
      sha256: 'p'.repeat(64), warnings: [], cli_equivalent: 'nova dashboard show w1',
    });
    const { onSaved } = renderEditor({ initialText: NEW });
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled(); // nothing validated yet
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));

    const preview = await screen.findByRole('region', { name: /Preview of w1\.widget\.json/ });
    expect(within(preview).getByText(/2 changed lines/)).toBeInTheDocument();
    const lines = [...preview.querySelectorAll('[data-diff]')].map((n) => n.getAttribute('data-diff'));
    expect(lines).toEqual(['same', 'same', 'del', 'add', 'same']);

    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(mocks.applyDashboardDocument).toHaveBeenCalledWith({ text: NEW, base_sha256: 'c'.repeat(64) });
    expect(await screen.findByTestId('saved')).toHaveTextContent(/written.*w1\.widget\.json/);
  });

  it('expects no file when creating (base_sha256 is the empty string)', async () => {
    mocks.validateDashboardDocument.mockResolvedValue(
      accepted({ action: 'create', existing: null, current_sha256: null }),
    );
    mocks.applyDashboardDocument.mockResolvedValue({
      ok: true, kind: 'widget', id: 'w1', file: 'w1.widget.json', changed: true,
      sha256: 'x', warnings: [], cli_equivalent: 'nova dashboard show w1',
    });
    renderEditor({ initialText: NEW });
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    expect(await screen.findByText(/New file w1\.widget\.json/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(mocks.applyDashboardDocument).toHaveBeenCalledWith({ text: NEW, base_sha256: '' }),
    );
  });

  it('withdraws the acceptance as soon as the text changes', async () => {
    mocks.validateDashboardDocument.mockResolvedValue(accepted());
    renderEditor({ initialText: NEW });
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    await screen.findByTestId('accepted');
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    await userEvent.type(box(), ' ');
    expect(screen.queryByTestId('accepted')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.getByText(/changed since it was validated/)).toBeInTheDocument();
  });

  it('offers no Save for an unchanged file and says so', async () => {
    mocks.validateDashboardDocument.mockResolvedValue(accepted({ action: 'unchanged', existing: NEW }));
    renderEditor({ initialText: NEW });
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    expect(await screen.findByText(/saving would change nothing/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('surfaces non-fatal warnings from the server', async () => {
    mocks.validateDashboardDocument.mockResolvedValue(accepted({ warnings: ['references widgets with no file yet: gone'] }));
    renderEditor({ initialText: NEW });
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    expect(await screen.findByRole('note')).toHaveTextContent('gone');
  });

  it.each([
    [409, 'the file changed since this preview was taken', /Out of date: the file changed/],
    [403, "credential holds 'read'; this route needs 'operate'", /Not allowed: credential holds 'read'/],
    [422, 'widget is invalid at presentation/chart: pie', /^widget is invalid at presentation\/chart: pie$/],
    [413, 'body exceeds the 262144-byte limit', /body exceeds/],
  ])('shows a %i from save verbatim and does not claim a write', async (status, message, shown) => {
    mocks.validateDashboardDocument.mockResolvedValue(accepted());
    mocks.applyDashboardDocument.mockRejectedValue(new mocks.ServeApiError(status, message));
    const { onSaved } = renderEditor({ initialText: NEW });
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(shown);
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.queryByTestId('saved')).not.toBeInTheDocument();
  });

  it('reports a failed validate request as an error, not as a verdict', async () => {
    mocks.validateDashboardDocument.mockRejectedValue(new mocks.ServeApiError(500, 'boom'));
    renderEditor({ initialText: NEW });
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('boom');
    expect(screen.queryByTestId('accepted')).not.toBeInTheDocument();
  });

  it('will not validate an empty box and says why', async () => {
    renderEditor();
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/Nothing to validate/);
    expect(mocks.validateDashboardDocument).not.toHaveBeenCalled();
  });

  it('Ctrl+Enter validates from the text box', async () => {
    mocks.validateDashboardDocument.mockResolvedValue(accepted());
    renderEditor({ initialText: NEW });
    box().focus();
    await userEvent.keyboard('{Control>}{Enter}{/Control}');
    await waitFor(() => expect(mocks.validateDashboardDocument).toHaveBeenCalledTimes(1));
  });

  it('Escape closes the dialog', async () => {
    const { onClose } = renderEditor();
    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalled();
  });

  it('reads an uploaded file into the editor', async () => {
    renderEditor();
    const file = new File([NEW], 'w1.widget.json', { type: 'application/json' });
    Object.defineProperty(file, 'text', { value: () => Promise.resolve(NEW) });
    await userEvent.upload(screen.getByLabelText(/upload a \.json file/i), file);
    await waitFor(() => expect(box()).toHaveValue(NEW));
  });

  it('is a roving tablist and the guided form sends composed JSON text', async () => {
    mocks.validateDashboardDocument.mockResolvedValue(accepted());
    renderEditor();
    const tabs = screen.getByRole('tablist', { name: 'How to provide the document' });
    const [jsonTab, guidedTab] = within(tabs).getAllByRole('tab');
    expect(jsonTab).toHaveAttribute('aria-selected', 'true');
    jsonTab!.focus();
    await userEvent.keyboard('{ArrowRight}');
    expect(guidedTab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel', { name: 'Guided widget' })).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText(/^Id/), 'cost-by-status');
    await userEvent.type(screen.getByLabelText(/^Title/), 'Cost');
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    const sent = mocks.validateDashboardDocument.mock.calls[0]![0] as { text: string };
    expect(JSON.parse(sent.text)).toMatchObject({ id: 'cost-by-status', title: 'Cost', presentation: { chart: 'bar' } });
  });

  it('flags a non-JSON query in the guided form without calling the server', async () => {
    renderEditor();
    await userEvent.click(screen.getByRole('tab', { name: 'Guided widget' }));
    const query = screen.getByLabelText(/Query \(ADR-0129 JSON\)/);
    await userEvent.clear(query);
    await userEvent.type(query, 'select count');
    expect(screen.getByText(/The query box is not JSON/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    expect(mocks.validateDashboardDocument).not.toHaveBeenCalled();
  });
});

describe('DashboardsView write entry points', () => {
  const LIST = {
    dashboards: [],
    widgets: [{ id: 'w1', title: 'Old', description: null, chart: 'bar', version: 1 }],
    invalid_files: [],
    cli_equivalent: 'nova dashboard list',
  };

  beforeEach(() => {
    mocks.listDashboards.mockReset().mockResolvedValue(LIST);
    mocks.getWidgetData.mockReset().mockReturnValue(new Promise(() => {}));
    mocks.exportDashboardDocument.mockReset();
  });

  it('opens the editor on an existing file with its verbatim bytes, and refreshes after a save', async () => {
    mocks.exportDashboardDocument.mockResolvedValue(new Blob([OLD]));
    Object.defineProperty(Blob.prototype, 'text', { configurable: true, value: () => Promise.resolve(OLD) });
    mocks.validateDashboardDocument.mockResolvedValue(accepted());
    mocks.applyDashboardDocument.mockResolvedValue({
      ok: true, kind: 'widget', id: 'w1', file: 'w1.widget.json', changed: true,
      sha256: 'x', warnings: [], cli_equivalent: 'nova dashboard show w1',
    });
    render(
      <ToastProvider>
        <DashboardsView />
      </ToastProvider>,
    );
    await userEvent.click(await screen.findByRole('button', { name: /Edit Old widget JSON/ }));
    expect(mocks.exportDashboardDocument).toHaveBeenCalledWith('widget', 'w1');
    await waitFor(() => expect(box()).toHaveValue(OLD));
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Save' }));
    await waitFor(() => expect(mocks.listDashboards.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('offers Add or import even when nothing is installed', async () => {
    mocks.listDashboards.mockResolvedValue({ ...LIST, widgets: [] });
    render(
      <ToastProvider>
        <DashboardsView />
      </ToastProvider>,
    );
    await userEvent.click(await screen.findByRole('button', { name: /Add or import/ }));
    expect(screen.getByRole('dialog', { name: /Add or edit a dashboard/ })).toBeInTheDocument();
  });
});
