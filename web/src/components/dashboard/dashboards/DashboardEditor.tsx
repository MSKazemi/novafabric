/**
 * Dashboard / widget editor — the UI's `nova dashboard validate` + `apply`
 * (ADR-0235; experimental).
 *
 * Contract this component keeps:
 * - **The server decides.** Every verdict comes from `POST /api/dashboards/validate`
 *   (the same loaders as the CLI: schema, then the ADR-0129 DSL allow-list).
 *   Nothing here re-implements a rule; a refusal is shown with the server's
 *   own reason. The guided form only composes JSON text.
 * - **No optimism.** Save is only offered for a document the server has just
 *   accepted *as typed*; editing afterwards withdraws the verdict. After saving,
 *   what is shown is the server's answer (written / unchanged), not an
 *   assumption. A stale preview is a 409 from the server, shown as such.
 * - **See before you save.** The preview is a diff against the bytes on disk
 *   (or the whole new file) — the exact bytes the server would store.
 */
import { useId, useState, type KeyboardEvent } from 'react';
import { ServeApiError, api } from '../../../lib/api';
import type { DashboardApplyResponse, DashboardValidateResponse } from '../../../lib/dashboardTypes';
import { useToast } from '../../../lib/ToastContext';
import Badge from '../../ui/primitives/Badge';
import Button from '../../ui/primitives/Button';
import Field from '../../ui/primitives/Field';
import Input from '../../ui/primitives/Input';
import Modal from '../../ui/primitives/Modal';
import SegmentedControl from '../../ui/primitives/SegmentedControl';
import Select from '../../ui/primitives/Select';
import Textarea from '../../ui/primitives/Textarea';
import { KNOWN_CHARTS } from './model';
import { EMPTY_WIDGET_FIELDS, composeWidgetText, lineDiff, type WidgetFields } from './editorModel';

type Mode = 'json' | 'guided';

const MODES = [
  { value: 'json', label: 'Paste or upload JSON' },
  { value: 'guided', label: 'Guided widget' },
] as const;

const ACTION_LABEL = { create: 'new file', update: 'changes an existing file', unchanged: 'no change' } as const;

function saveErrorText(e: unknown): string {
  if (e instanceof ServeApiError) {
    if (e.status === 403) return `Not allowed: ${e.message}`;
    if (e.status === 409) return `Out of date: ${e.message}`;
    return e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

function DiffView({ verdict }: { verdict: DashboardValidateResponse }) {
  const fileName = `${verdict.id}.${verdict.kind}.json`;
  const lines = lineDiff(verdict.existing ?? null, verdict.normalized ?? '');
  const changed = lines.filter((l) => l.kind !== 'same').length;
  return (
    <section aria-label={`Preview of ${fileName}`} className="space-y-1">
      <p className="text-2xs text-[var(--color-text-faint)]">
        {verdict.action === 'create'
          ? `New file ${fileName} — exactly these bytes will be stored.`
          : verdict.action === 'unchanged'
            ? `${fileName} already holds exactly these bytes; saving would change nothing.`
            : `${changed} changed line${changed === 1 ? '' : 's'} in ${fileName} — exactly these bytes will be stored.`}
      </p>
      <pre
        tabIndex={0}
        className="max-h-64 overflow-auto rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-2 text-2xs font-mono leading-relaxed focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-accent)]"
      >
        {lines.map((l, i) => (
          <span
            key={i}
            data-diff={l.kind}
            className={`block whitespace-pre-wrap break-all ${
              l.kind === 'add'
                ? 'bg-[var(--color-success-tint)] text-[var(--color-status-success)]'
                : l.kind === 'del'
                  ? 'bg-[var(--color-danger-tint)] text-[var(--color-status-failure)]'
                  : 'text-[var(--color-text-muted)]'
            }`}
          >
            {l.kind === 'add' ? '+ ' : l.kind === 'del' ? '- ' : '  '}
            {l.text}
          </span>
        ))}
      </pre>
    </section>
  );
}

export interface DashboardEditorProps {
  /** Pre-filled JSON (e.g. an existing file being edited). */
  initialText?: string;
  /** Called after the server accepted a write, so the lists can refresh. */
  onSaved: (result: DashboardApplyResponse) => void;
  onClose: () => void;
}

export default function DashboardEditor({ initialText = '', onSaved, onClose }: DashboardEditorProps) {
  const { toast } = useToast();
  const uid = useId();
  const [mode, setMode] = useState<Mode>('json');
  const [text, setText] = useState(initialText);
  const [fields, setFields] = useState<WidgetFields>(EMPTY_WIDGET_FIELDS);
  const [verdict, setVerdict] = useState<DashboardValidateResponse | null>(null);
  const [verdictInput, setVerdictInput] = useState<string | null>(null);
  const [validating, setValidating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [saved, setSaved] = useState<DashboardApplyResponse | null>(null);

  const composed = mode === 'guided' ? composeWidgetText(fields) : { text };
  const input = 'text' in composed ? composed.text : null;
  const inputError = 'error' in composed ? composed.error : null;
  const stale = verdict !== null && verdictInput !== input;
  const accepted = verdict?.ok === true && !stale ? verdict : null;
  const canSave = accepted !== null && accepted.action !== 'unchanged' && !saving && saved === null;

  function edited() {
    // Any edit withdraws the verdict and any earlier outcome: what is on screen
    // must always describe what is in the editor.
    setRequestError(null);
    setSaved(null);
  }

  async function validate() {
    if (input === null || !input.trim()) {
      setRequestError(inputError ?? 'Nothing to validate yet — paste JSON, upload a file, or fill in the fields.');
      return;
    }
    setValidating(true);
    setRequestError(null);
    setSaved(null);
    try {
      const v = await api.validateDashboardDocument({ text: input });
      setVerdict(v);
      setVerdictInput(input);
    } catch (e) {
      setVerdict(null);
      setRequestError(saveErrorText(e));
    } finally {
      setValidating(false);
    }
  }

  async function save() {
    if (!accepted || input === null) return;
    setSaving(true);
    setRequestError(null);
    try {
      const res = await api.applyDashboardDocument({
        text: input,
        // The preview the person reviewed: a changed file is a 409, not an overwrite.
        base_sha256: accepted.current_sha256 ?? '',
      });
      setSaved(res);
      toast('success', res.changed ? `Saved ${res.file}` : `${res.file} was already up to date`);
      onSaved(res);
    } catch (e) {
      setRequestError(saveErrorText(e));
    } finally {
      setSaving(false);
    }
  }

  async function onFile(file: File | undefined) {
    if (!file) return;
    try {
      setText(await file.text());
      setVerdict(null);
      edited();
    } catch (e) {
      setRequestError(`Could not read ${file.name}: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  const onCtrlEnter = (e: KeyboardEvent) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !validating) {
      e.preventDefault();
      void validate();
    }
  };

  const setField = <K extends keyof WidgetFields>(key: K, value: WidgetFields[K]) => {
    setFields((f) => ({ ...f, [key]: value }));
    edited();
  };

  return (
    <Modal
      title="Add or edit a dashboard / widget"
      onClose={onClose}
      locked={validating || saving}
      widthClass="max-w-3xl"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={validating || saving}>
            {saved ? 'Close' : 'Cancel'}
          </Button>
          <Button onClick={validate} pending={validating} disabled={saved !== null}>
            Validate
          </Button>
          <Button variant="primary" onClick={save} pending={saving} disabled={!canSave} aria-describedby={`${uid}-hint`}>
            Save
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-xs">
        <p className="text-[var(--color-text-muted)]">
          Validate asks the server (the same check as <code className="font-mono">nova dashboard validate</code>) and
          writes nothing. Save is offered only for a document the server has just accepted, and stores exactly the
          bytes shown in the preview. Requires operate scope.
        </p>
        <SegmentedControl
          aria-label="How to provide the document"
          segments={MODES}
          value={mode}
          onChange={(m) => {
            setMode(m);
            edited();
          }}
        />

        {mode === 'json' ? (
          <div role="tabpanel" aria-label="Paste or upload JSON" className="space-y-2">
            <Field
              label="Widget or dashboard JSON"
              description="Ctrl/Cmd+Enter validates. A dashboard is recognised by its $novafabricDashboard marker."
            >
              {({ id, describedBy }) => (
                <Textarea
                  id={id}
                  aria-describedby={describedBy}
                  rows={12}
                  spellCheck={false}
                  value={text}
                  onChange={(e) => {
                    setText(e.target.value);
                    edited();
                  }}
                  onKeyDown={onCtrlEnter}
                />
              )}
            </Field>
            <Field label="Or upload a .json file">
              {({ id }) => (
                <input
                  id={id}
                  type="file"
                  accept=".json,application/json"
                  onChange={(e) => void onFile(e.target.files?.[0])}
                  className="block text-2xs text-[var(--color-text-muted)]"
                />
              )}
            </Field>
          </div>
        ) : (
          <div role="tabpanel" aria-label="Guided widget" className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <Field label="Id" required description="Becomes the file name. The server checks it.">
              {({ id, describedBy }) => (
                <Input id={id} aria-describedby={describedBy} value={fields.id} onChange={(e) => setField('id', e.target.value)} />
              )}
            </Field>
            <Field label="Title" required>
              {({ id }) => <Input id={id} value={fields.title} onChange={(e) => setField('title', e.target.value)} />}
            </Field>
            <Field label="Chart">
              {({ id }) => (
                <Select id={id} value={fields.chart} onChange={(e) => setField('chart', e.target.value)}>
                  {KNOWN_CHARTS.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Unit">
              {({ id }) => <Input id={id} value={fields.unit} onChange={(e) => setField('unit', e.target.value)} />}
            </Field>
            <Field label="Breakdown (a group-by column)">
              {({ id }) => (
                <Input id={id} value={fields.breakdown} onChange={(e) => setField('breakdown', e.target.value)} />
              )}
            </Field>
            <label className="flex items-center gap-2 self-end pb-1.5 text-xs text-[var(--color-text-muted)]">
              <input
                type="checkbox"
                checked={fields.stacked}
                onChange={(e) => setField('stacked', e.target.checked)}
              />
              Stacked
            </label>
            <Field label="Description" className="sm:col-span-2">
              {({ id }) => (
                <Input id={id} value={fields.description} onChange={(e) => setField('description', e.target.value)} />
              )}
            </Field>
            <Field
              label="Query (ADR-0129 JSON)"
              className="sm:col-span-2"
              error={inputError}
              description="The query DSL decides what is allowed; a query it refuses is shown below with its reason."
            >
              {({ id, describedBy }) => (
                <Textarea
                  id={id}
                  aria-describedby={describedBy}
                  invalid={inputError !== null}
                  rows={6}
                  spellCheck={false}
                  value={fields.query}
                  onChange={(e) => setField('query', e.target.value)}
                  onKeyDown={onCtrlEnter}
                />
              )}
            </Field>
          </div>
        )}

        <p id={`${uid}-hint`} className="text-2xs text-[var(--color-text-faint)]">
          {saved
            ? 'Saved. Close to return to the list, or edit and validate again.'
            : stale
              ? 'The document changed since it was validated — validate again to save.'
              : accepted
                ? accepted.action === 'unchanged'
                  ? 'Nothing to save: the file already matches.'
                  : 'Accepted by the server. Review the preview, then Save.'
                : 'Save becomes available once the server accepts the document.'}
        </p>

        {requestError && (
          <div
            role="alert"
            className="rounded border border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)] bg-[var(--color-danger-tint)] px-3 py-2 text-xs text-[var(--color-status-failure)] break-words"
          >
            {requestError}
          </div>
        )}

        {verdict && !stale && !verdict.ok && (
          <div
            role="alert"
            data-testid="refusal"
            className="rounded border border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)] bg-[var(--color-danger-tint)] px-3 py-2 space-y-1"
          >
            <Badge tone="danger" dot>
              refused — nothing written
            </Badge>
            <p className="font-mono text-2xs text-[var(--color-text)] break-words">{verdict.error}</p>
          </div>
        )}

        {accepted && (
          <div className="space-y-2" data-testid="accepted">
            <div role="status" className="flex flex-wrap items-center gap-2">
              <Badge tone="success" dot>
                accepted
              </Badge>
              <span>
                {accepted.kind} <code className="font-mono">{accepted.id}</code> — {accepted.title} —{' '}
                {ACTION_LABEL[accepted.action ?? 'update']}
              </span>
            </div>
            {accepted.warnings.map((w) => (
              <p key={w} role="note" className="text-2xs text-[var(--color-status-pending)] break-words">
                Note: {w}
              </p>
            ))}
            <DiffView verdict={accepted} />
          </div>
        )}

        {saved && (
          <div role="status" data-testid="saved" className="rounded border border-[var(--color-border)] px-3 py-2">
            <Badge tone={saved.changed ? 'success' : 'neutral'} dot>
              {saved.changed ? 'written' : 'unchanged'}
            </Badge>{' '}
            <span className="font-mono text-2xs">{saved.file}</span>
            <span className="block mt-1 text-2xs font-mono text-[var(--color-text-faint)]">$ {saved.cli_equivalent}</span>
          </div>
        )}
      </div>
    </Modal>
  );
}
