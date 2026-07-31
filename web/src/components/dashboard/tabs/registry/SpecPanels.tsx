/**
 * Spec utility panels: `nova validate <spec.yaml>` and `nova report`.
 * Extracted verbatim from the former RegistryTab monolith — behavior frozen.
 */
import { useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';

// ValidateSpecPanel — mirrors `nova validate <spec.yaml>`
export function ValidateSpecPanel({ onFlash }: { onFlash: (tone: 'success' | 'error', text: string) => void }) {
  const [specYaml, setSpecYaml] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; valid: boolean; errors: string[]; note: string } | null>(null);

  const handleValidate = async () => {
    if (!specYaml.trim()) { onFlash('error', 'Paste a YAML spec first'); return; }
    setBusy(true);
    setResult(null);
    try {
      const res = await api.validateSpec(specYaml);
      setResult(res);
    } catch (e) {
      onFlash('error', `Validate request failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const inputClass = 'w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 font-mono text-xs focus:border-[var(--color-accent)] focus:outline-none';

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Validate spec</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            CLI equivalent: <code className="font-mono">nova validate &lt;spec.yaml&gt;</code>
          </p>
        </div>
      </div>
      <label className="block">
        <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">YAML spec</span>
        <textarea
          value={specYaml}
          onChange={e => { setSpecYaml(e.target.value); setResult(null); }}
          placeholder={`name: my-agent\nversion: "1.0.0"\nasset_type: agent\ndescription: "My agent"\nowner: team@example.com`}
          rows={6}
          className={`mt-1 ${inputClass}`}
        />
      </label>
      <button
        onClick={handleValidate}
        disabled={busy || !specYaml.trim()}
        className="px-3 py-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-xs font-medium text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        {busy ? 'Validating…' : 'Validate'}
      </button>
      {result && (
        <div className={clsx(
          'rounded border px-3 py-2 text-xs space-y-1',
          result.valid
            ? 'border-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
            : 'border-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
        )}>
          <p className={clsx('font-medium', result.valid ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
            {result.valid ? 'Valid spec' : 'Invalid spec'}
          </p>
          {result.note && <p className="text-[var(--color-text-muted)]">{result.note}</p>}
          {result.errors.length > 0 && (
            <ul className="list-disc list-inside space-y-0.5 text-[var(--color-status-failure)] text-[11px]">
              {result.errors.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// ReportPanel — mirrors `nova report`
export function ReportPanel({ onFlash }: { onFlash: (tone: 'success' | 'error', text: string) => void }) {
  const [format, setFormat] = useState<'json' | 'markdown'>('json');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; format: string; content: string; note: string } | null>(null);

  const handleGenerate = async () => {
    setBusy(true);
    setResult(null);
    try {
      const res = await api.assetReport(format);
      setResult(res);
      if (!res.ok) onFlash('error', `Report error: ${res.note}`);
    } catch (e) {
      onFlash('error', `Report request failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Asset inventory report</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            CLI equivalent: <code className="font-mono">nova report --format {format}</code>
          </p>
        </div>
      </div>
      <div className="flex items-center gap-3">
        <label className="block">
          <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Format</span>
          <select
            value={format}
            onChange={e => { setFormat(e.target.value as 'json' | 'markdown'); setResult(null); }}
            className="mt-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-xs"
          >
            <option value="json">json</option>
            <option value="markdown">markdown</option>
          </select>
        </label>
        <button
          onClick={handleGenerate}
          disabled={busy}
          className="mt-5 px-3 py-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-xs font-medium text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {busy ? 'Generating…' : 'Generate report'}
        </button>
      </div>
      {result && (
        <div className="space-y-1">
          <p className={clsx('text-[10px] font-medium', result.ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
            {result.note}
          </p>
          {result.content && (
            <pre className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 text-[10px] font-mono text-[var(--color-text-muted)] overflow-auto max-h-64 whitespace-pre-wrap break-all">
              {result.content}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
