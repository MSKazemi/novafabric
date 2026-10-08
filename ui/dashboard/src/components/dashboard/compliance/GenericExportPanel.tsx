/**
 * Generic compliance-export panel (ADR-0200 §2).
 *
 * One dynamic panel for every registry export kind: the form is rendered from
 * the server-driven catalog (`GET /api/compliance/export/kinds`), so newly
 * registered kinds appear here with zero TypeScript changes. Mirrors the
 * bespoke ComplianceTab panel idioms — result JSON pretty-print + download,
 * zip download via base64→Blob, and the CLI-equivalent line.
 */

import { useState, useEffect, useMemo, useCallback } from 'react';
import { SuggestInput } from '../../ui/SuggestInput';
import CopyButton from '../../ui/CopyButton';
import {
  fetchExportKinds,
  runExport,
  type ExportKindSpec,
  type ExportRunResult,
} from './exportApi';

export default function GenericExportPanel({ runIds }: { runIds: string[] }) {
  const [kinds, setKinds] = useState<ExportKindSpec[] | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string>('');
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ExportRunResult | null>(null);

  useEffect(() => {
    fetchExportKinds()
      .then(r => {
        setKinds(r.kinds);
        if (r.kinds.length > 0) setSelected(r.kinds[0].kind);
      })
      .catch(e => setCatalogError((e as Error).message));
  }, []);

  const spec = useMemo(
    () => kinds?.find(k => k.kind === selected) ?? null,
    [kinds, selected],
  );

  const selectKind = useCallback((kind: string) => {
    setSelected(kind);
    setValues({});
    setResult(null);
    setError(null);
  }, []);

  const setField = useCallback((key: string, v: string | boolean) => {
    setValues(prev => ({ ...prev, [key]: v }));
  }, []);

  const missingRequired = useMemo(() => {
    if (!spec) return [];
    return spec.fields
      .filter(f => f.required)
      .filter(f => !String(values[f.key] ?? '').trim())
      .map(f => f.key);
  }, [spec, values]);

  const run = useCallback(async () => {
    if (!spec) return;
    setLoading(true);
    setError(null);
    setResult(null);
    const body: Record<string, unknown> = {};
    if (runId.trim()) body.run_id = runId.trim();
    try {
      for (const f of spec.fields) {
        const raw = values[f.key];
        if (f.type === 'boolean') {
          if (raw === true) body[f.key] = true;
          continue;
        }
        const text = String(raw ?? '').trim();
        if (!text) continue;
        if (f.type === 'json') {
          try {
            body[f.key] = JSON.parse(text);
          } catch {
            throw new Error(`field "${f.key}" is not valid JSON`);
          }
        } else {
          body[f.key] = text;
        }
      }
      const r = await runExport(spec.kind, body);
      setResult(r);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [spec, values, runId]);

  const downloadDocument = useCallback(() => {
    if (!result?.document) return;
    const blob = new Blob([JSON.stringify(result.document, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${result.kind}-export.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [result]);

  const downloadZip = useCallback(() => {
    if (!result?.zip_base64) return;
    const blob = new Blob([Uint8Array.from(atob(result.zip_base64), c => c.charCodeAt(0))], {
      type: 'application/zip',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = result.filename ?? `${result.kind}-export.zip`;
    a.click();
    URL.revokeObjectURL(url);
  }, [result]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">More exports (registry)</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Generic compliance-export registry — one form per <code className="font-mono">nova export-*</code> kind, server-driven
          </p>
        </div>
        <span className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] shrink-0">
          ADR-0200
        </span>
      </div>

      {catalogError && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {catalogError}
        </div>
      )}

      {kinds && (
        <>
          <div className="flex flex-wrap gap-2 items-end">
            <label className="block space-y-1 min-w-56">
              <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Export kind</span>
              <select
                value={selected}
                onChange={e => selectKind(e.target.value)}
                className="block w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 focus:border-[var(--color-accent)] focus:outline-none"
              >
                {kinds.map(k => (
                  <option key={k.kind} value={k.kind}>{k.label}</option>
                ))}
              </select>
            </label>
            <label className="block space-y-1 flex-1 min-w-48">
              <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">run_id (optional)</span>
              <SuggestInput
                value={runId}
                onChange={setRunId}
                suggestions={runIds}
                placeholder="validate against a capsule (optional)"
                className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
              />
            </label>
          </div>

          {spec && (
            <>
              <p className="text-[10px] italic text-[var(--color-text-faint)]">{spec.note}</p>

              <div className="grid gap-2 sm:grid-cols-2">
                {spec.fields.map(f => (
                  <label key={f.key} className={`block space-y-1 ${f.type === 'json' ? 'sm:col-span-2' : ''}`}>
                    <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
                      {f.label}
                      {f.required && <span className="text-[var(--color-status-failure)]"> *</span>}
                    </span>
                    {f.type === 'boolean' ? (
                      <span className="flex items-center gap-2 py-1">
                        <input
                          type="checkbox"
                          checked={values[f.key] === true}
                          onChange={e => setField(f.key, e.target.checked)}
                        />
                        <span className="text-[10px] text-[var(--color-text-muted)] font-mono">{f.key}</span>
                      </span>
                    ) : f.type === 'json' ? (
                      <textarea
                        value={String(values[f.key] ?? '')}
                        onChange={e => setField(f.key, e.target.value)}
                        placeholder='JSON — e.g. [] or {}'
                        rows={2}
                        className="block w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
                      />
                    ) : (
                      <input
                        type="text"
                        value={String(values[f.key] ?? '')}
                        onChange={e => setField(f.key, e.target.value)}
                        placeholder={f.key}
                        className="block w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
                      />
                    )}
                  </label>
                ))}
              </div>

              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={run}
                  disabled={loading || missingRequired.length > 0}
                  className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  {loading ? 'Exporting…' : 'Run export'}
                </button>
                {missingRequired.length > 0 && (
                  <span className="text-[10px] text-[var(--color-text-faint)]">
                    required: {missingRequired.join(', ')}
                  </span>
                )}
              </div>

              <div className="font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
                $ {spec.cli_equivalent}
              </div>
            </>
          )}

          {error && (
            <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
              {error}
            </div>
          )}

          {result?.ok && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-[10px] font-semibold text-[var(--color-status-success)]">export rendered</span>
                {result.run_id && (
                  <span className="text-[9px] font-mono text-[var(--color-text-faint)]">{result.run_id}</span>
                )}
                {result.document && (
                  <>
                    <CopyButton text={JSON.stringify(result.document, null, 2)} />
                    <button
                      type="button"
                      onClick={downloadDocument}
                      className="px-2 py-0.5 text-[10px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)] transition-colors"
                    >
                      download JSON
                    </button>
                  </>
                )}
                {result.zip_base64 && (
                  <button
                    type="button"
                    onClick={downloadZip}
                    className="px-2 py-0.5 text-[10px] rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)] transition-colors"
                  >
                    download zip
                  </button>
                )}
              </div>
              {result.document && (
                <pre className="max-h-72 overflow-auto text-[10px] font-mono bg-[var(--color-bg-sunken)] border border-[var(--color-border)] rounded px-3 py-2 whitespace-pre-wrap break-all">
                  {JSON.stringify(result.document, null, 2)}
                </pre>
              )}
              <p className="text-[10px] italic text-[var(--color-text-faint)]">{result.note}</p>
            </div>
          )}
        </>
      )}
    </section>
  );
}
