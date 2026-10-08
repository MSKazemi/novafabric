import { Component, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { clsx } from 'clsx';
import { capsules } from '../../lib/fixtures';
import { api } from '../../lib/api';
import type { RunSummary } from '../../lib/api';
import { validateCapsule } from '../../lib/validate';
import {
  normalizeModelCall,
  normalizeToolCall,
} from '../../lib/traceUtils';
import { runLabel, shortTs } from '../../lib/runUtils';

// ─── Error boundary ───────────────────────────────────────────────────────────

class RenderErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(e: Error) { return { error: e }; }
  render() {
    if (this.state.error) {
      return (
        <div className="p-4 rounded border border-[var(--color-status-failure)] bg-[var(--color-bg-raised)]">
          <p className="text-sm font-medium text-[var(--color-status-failure)] mb-2">Render error</p>
          <pre className="text-xs font-mono text-[var(--color-text-muted)] overflow-x-auto whitespace-pre-wrap break-all">
            {this.state.error.message}
          </pre>
        </div>
      );
    }
    return this.props.children;
  }
}

type FileKey = 'capsule.yaml' | 'trace.jsonl' | 'model-calls.jsonl' | 'tool-calls.jsonl' | 'env.lock' | 'redaction-proof.json';

interface FileMeta {
  key: FileKey;
  label: string;
  size: string;
  language: 'yaml' | 'jsonl' | 'json' | 'env';
}

const FILES: FileMeta[] = [
  { key: 'capsule.yaml', label: 'capsule.yaml', size: '1.8 KB', language: 'yaml' },
  { key: 'trace.jsonl', label: 'trace.jsonl', size: '0.6 KB', language: 'jsonl' },
  { key: 'model-calls.jsonl', label: 'model-calls.jsonl', size: '1.2 KB', language: 'jsonl' },
  { key: 'tool-calls.jsonl', label: 'tool-calls.jsonl', size: '0.9 KB', language: 'jsonl' },
  { key: 'env.lock', label: 'env.lock', size: '4.7 KB', language: 'env' },
  { key: 'redaction-proof.json', label: 'redaction-proof.json', size: '1.9 KB', language: 'json' },
];

interface IOFileMeta {
  name: string;
  size_bytes: number;
}

interface CapsuleData {
  capsule: Record<string, unknown>;
  capsuleYaml: string;
  modelCalls: unknown[];
  toolCalls: unknown[];
  trace: unknown[];
  inputs?: IOFileMeta[];
  outputs?: IOFileMeta[];
}

interface CapsuleInspectorProps {
  /** Optional capsule to inspect. Falls back to the showcase fixture (RUN_A). */
  capsuleData?: CapsuleData;
  /** When provided, shows a "Compare against…" button. Called with (thisRunId, selectedRunId). */
  onCompareTo?: (a: string, b: string) => void;
}

function makeYamlFromManifest(manifest: Record<string, unknown>): string {
  // Lightweight stable serialiser for live data when raw YAML isn't available.
  // Order keys for human readability; quote strings; render arrays inline.
  const entries: string[] = [];
  for (const [k, v] of Object.entries(manifest)) {
    if (v === null || v === undefined) continue;
    if (Array.isArray(v)) {
      if (v.every((x) => typeof x === 'string' || typeof x === 'number')) {
        entries.push(`${k}: ${JSON.stringify(v)}`);
      } else {
        entries.push(`${k}: ${JSON.stringify(v, null, 2)}`);
      }
    } else if (typeof v === 'object') {
      const sub = Object.entries(v as Record<string, unknown>)
        .map(([sk, sv]) => `  ${sk}: ${JSON.stringify(sv)}`)
        .join('\n');
      entries.push(`${k}:\n${sub}`);
    } else if (typeof v === 'string') {
      entries.push(`${k}: ${JSON.stringify(v)}`);
    } else {
      entries.push(`${k}: ${v}`);
    }
  }
  return entries.join('\n');
}

export default function CapsuleInspector({ capsuleData, onCompareTo }: CapsuleInspectorProps = {}) {
  const [active, setActive] = useState<FileKey>('capsule.yaml');
  const [hoveredSpan, setHoveredSpan] = useState<string | null>(null);
  const [validation, setValidation] = useState<null | { ok: boolean; durationMs: number; errors?: Array<{ path: string; message: string; keyword: string }> }>(null);
  const [ioFile, setIoFile] = useState<string | null>(null);
  const [ioContent, setIoContent] = useState<string | null>(null);
  const [ioLoading, setIoLoading] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerRuns, setPickerRuns] = useState<RunSummary[] | null>(null);
  const [pickerSearch, setPickerSearch] = useState('');
  const pickerRef = useRef<HTMLDivElement>(null);
  const runsLoadedRef = useRef(false);

  const data: CapsuleData = useMemo(() => {
    if (capsuleData) {
      return {
        ...capsuleData,
        capsuleYaml: capsuleData.capsuleYaml || makeYamlFromManifest(capsuleData.capsule),
      };
    }
    return capsules.RUN_A;
  }, [capsuleData]);

  const onValidate = () => {
    const result = validateCapsule(data.capsule);
    setValidation(result);
  };

  const traceLines = useMemo(() => data.trace, [data]);
  const modelLines = useMemo(() => data.modelCalls, [data]);
  const toolLines = useMemo(() => data.toolCalls, [data]);
  const inputFiles = data.inputs ?? [];
  const outputFiles = data.outputs ?? [];

  const runId = (data.capsule as { run_id?: string }).run_id ?? '';

  const handleOpenPicker = useCallback(async () => {
    setPickerOpen(o => !o);
    if (!runsLoadedRef.current) {
      runsLoadedRef.current = true;
      try {
        const r = await api.listRuns();
        const sorted = [...r.runs].sort((x, y) => (y.created_at ?? '').localeCompare(x.created_at ?? ''));
        setPickerRuns(sorted);
      } catch {
        setPickerRuns([]);
      }
    }
  }, []);

  useEffect(() => {
    if (!pickerOpen) return;
    function onMouseDown(e: MouseEvent) {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setPickerOpen(false);
      }
    }
    document.addEventListener('mousedown', onMouseDown);
    return () => document.removeEventListener('mousedown', onMouseDown);
  }, [pickerOpen]);

  const handleIOFileClick = useCallback(async (filepath: string) => {
    setIoFile(filepath);
    setIoContent(null);
    setIoLoading(true);
    const run_id = (data.capsule as { run_id?: string }).run_id;
    if (!run_id) {
      setIoContent('(run_id not available)');
      setIoLoading(false);
      return;
    }
    try {
      const result = await api.getRunFile(run_id, filepath);
      setIoContent(result.content);
    } catch {
      setIoContent('(error loading file)');
    } finally {
      setIoLoading(false);
    }
  }, [data.capsule]);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[260px_1fr] gap-4">
      {/* Left — file tree */}
      <aside className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3">
        <div className="px-2 py-1.5 mb-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono">
          .novafabric/runs/{(data.capsule as { run_id: string }).run_id.slice(0, 8)}…/
        </div>
        <ul className="space-y-0.5">
          {FILES.map((f) => (
            <li key={f.key}>
              <button
                type="button"
                onClick={() => { setActive(f.key); setIoFile(null); }}
                className={clsx(
                  'w-full flex items-center justify-between gap-2 px-2 py-1.5 rounded text-sm transition-colors text-left',
                  active === f.key && ioFile === null
                    ? 'bg-[var(--color-bg-sunken)] text-[var(--color-text)]'
                    : 'text-[var(--color-text-muted)] hover:bg-[var(--color-bg-sunken)] hover:text-[var(--color-text)]',
                )}
              >
                <span className="font-mono truncate">{f.label}</span>
                <span className="text-[10px] text-[var(--color-text-faint)] shrink-0">{f.size}</span>
              </button>
            </li>
          ))}
        </ul>

        {(inputFiles.length > 0 || outputFiles.length > 0) && (
          <div className="mt-4 pt-3 border-t border-[var(--color-border)]">
            {inputFiles.length > 0 && (
              <>
                <p className="px-2 mb-0.5 text-[10px] text-[var(--color-text-faint)] font-mono">inputs/</p>
                <ul className="space-y-0.5">
                  {inputFiles.map((f) => {
                    const filepath = `inputs/${f.name}`;
                    return (
                      <li key={filepath}>
                        <button
                          type="button"
                          onClick={() => handleIOFileClick(filepath)}
                          className={clsx(
                            'w-full flex items-center justify-between gap-2 px-2 py-1.5 rounded text-sm transition-colors text-left',
                            ioFile === filepath
                              ? 'bg-[var(--color-bg-sunken)] text-[var(--color-text)]'
                              : 'text-[var(--color-text-muted)] hover:bg-[var(--color-bg-sunken)] hover:text-[var(--color-text)]',
                          )}
                        >
                          <span className="font-mono truncate pl-2">{f.name}</span>
                          <span className="text-[10px] text-[var(--color-text-faint)] shrink-0">{(f.size_bytes / 1024).toFixed(1)} KB</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
            {outputFiles.length > 0 && (
              <>
                <p className="px-2 mt-2 mb-0.5 text-[10px] text-[var(--color-text-faint)] font-mono">outputs/</p>
                <ul className="space-y-0.5">
                  {outputFiles.map((f) => {
                    const filepath = `outputs/${f.name}`;
                    return (
                      <li key={filepath}>
                        <button
                          type="button"
                          onClick={() => handleIOFileClick(filepath)}
                          className={clsx(
                            'w-full flex items-center justify-between gap-2 px-2 py-1.5 rounded text-sm transition-colors text-left',
                            ioFile === filepath
                              ? 'bg-[var(--color-bg-sunken)] text-[var(--color-text)]'
                              : 'text-[var(--color-text-muted)] hover:bg-[var(--color-bg-sunken)] hover:text-[var(--color-text)]',
                          )}
                        >
                          <span className="font-mono truncate pl-2">{f.name}</span>
                          <span className="text-[10px] text-[var(--color-text-faint)] shrink-0">{(f.size_bytes / 1024).toFixed(1)} KB</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
          </div>
        )}
      </aside>

      {/* Right — file viewer */}
      <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] overflow-hidden flex flex-col min-h-[520px]">
        <header className="flex items-center justify-between px-4 py-3 border-b border-[var(--color-border)] bg-[var(--color-bg-raised)]">
          <div className="flex items-center gap-3">
            <code className="font-mono text-sm text-[var(--color-text)]">{ioFile ?? active}</code>
            {ioFile === null && (
              <span className="text-xs text-[var(--color-text-faint)] uppercase tracking-wider">
                {FILES.find((f) => f.key === active)?.language}
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            {onCompareTo && runId && (
              <div className="relative" ref={pickerRef}>
                <button
                  type="button"
                  onClick={handleOpenPicker}
                  className={clsx(
                    'inline-flex items-center gap-1.5 px-3 py-1 rounded text-xs font-medium border transition-colors',
                    pickerOpen
                      ? 'border-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] text-[var(--color-accent)]'
                      : 'border-[var(--color-border)] hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
                  )}
                >
                  Compare against…
                </button>
                {pickerOpen && (
                  <div className="absolute right-0 top-full mt-1 z-50 w-72 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-lg overflow-hidden">
                    <div className="p-2 border-b border-[var(--color-border)]">
                      <input
                        autoFocus
                        type="search"
                        value={pickerSearch}
                        onChange={e => setPickerSearch(e.target.value)}
                        onKeyDown={e => e.key === 'Escape' && setPickerOpen(false)}
                        placeholder="Filter runs…"
                        className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1 font-mono focus:border-[var(--color-accent)] focus:outline-none"
                      />
                    </div>
                    <ul className="max-h-52 overflow-y-auto divide-y divide-[var(--color-border)]">
                      {pickerRuns === null ? (
                        <li className="px-3 py-2 text-xs text-[var(--color-text-faint)]">Loading…</li>
                      ) : pickerRuns.filter(r =>
                        r.run_id !== runId &&
                        (!pickerSearch.trim() || r.run_id.includes(pickerSearch) || runLabel(r).toLowerCase().includes(pickerSearch.toLowerCase()))
                      ).slice(0, 12).map(r => (
                        <li key={r.run_id}>
                          <button
                            type="button"
                            onClick={() => { setPickerOpen(false); onCompareTo(runId, r.run_id); }}
                            className="w-full flex items-center gap-2 px-3 py-2 text-xs hover:bg-[var(--color-bg-sunken)] transition-colors text-left"
                          >
                            <span className={clsx('inline-block w-2 h-2 rounded-full shrink-0', r.exit_code === 0 || r.exit_code === null ? 'bg-[var(--color-status-success)]' : 'bg-[var(--color-status-failure)]')} />
                            <span className="flex-1 truncate font-mono text-[var(--color-text)]">{runLabel(r)}</span>
                            <span className="shrink-0 text-[10px] text-[var(--color-text-faint)]">{shortTs(r.created_at)}</span>
                          </button>
                        </li>
                      ))}
                      {pickerRuns !== null && pickerRuns.filter(r => r.run_id !== runId).length === 0 && (
                        <li className="px-3 py-2 text-xs text-[var(--color-text-faint)]">No other runs available.</li>
                      )}
                    </ul>
                  </div>
                )}
              </div>
            )}
            {ioFile === null && validation && (
              <span
                className={clsx(
                  'inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs font-medium',
                  validation.ok
                    ? 'bg-[color-mix(in_oklab,var(--color-status-success)_15%,transparent)] text-[var(--color-status-success)]'
                    : 'bg-[color-mix(in_oklab,var(--color-status-failure)_15%,transparent)] text-[var(--color-status-failure)]',
                )}
              >
                {validation.ok ? '✓' : '✗'}
                {validation.ok
                  ? `Valid · ${validation.durationMs.toFixed(1)}ms`
                  : `${validation.errors?.length ?? 0} errors`}
              </span>
            )}
            {ioFile === null && (
              <button
                type="button"
                onClick={onValidate}
                className="inline-flex items-center gap-1.5 px-3 py-1 rounded text-xs font-medium border border-[var(--color-border)] hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
                title="Run Ajv against run-capsule.schema.json in your browser"
              >
                Validate against schema
              </button>
            )}
          </div>
        </header>

        <div className="flex-1 overflow-auto p-4 font-mono text-sm leading-relaxed">
          <RenderErrorBoundary>
          {active === 'capsule.yaml' && (
            <pre className="whitespace-pre text-[var(--color-text)]">{data.capsuleYaml}</pre>
          )}

          {active === 'trace.jsonl' && (
            <TraceSpanView
              spans={traceLines}
              hoveredSpan={hoveredSpan}
              onHover={setHoveredSpan}
            />
          )}

          {active === 'model-calls.jsonl' && (
            <ModelCallsView calls={modelLines} hoveredSpan={hoveredSpan} />
          )}

          {active === 'tool-calls.jsonl' && (
            <ToolCallsView calls={toolLines} hoveredSpan={hoveredSpan} />
          )}

          {active === 'env.lock' && (
            <pre className="whitespace-pre text-[var(--color-text-muted)]">
{`schema_version: 0.1.0
host:
  arch: x86_64
  os: Linux 6.14.0
  python: 3.12.4
  cpu_count: 16
  memory_bytes: 67108864000
  gpu: null
runtime:
  novafabric: 0.6.12
  openai: 1.55.0
  anthropic: 0.45.0
  mcp: 1.2.1
  pydantic: 2.10.4
  requests: 2.32.3
locale:
  LANG: C.UTF-8
  TZ: UTC
captured_at: "2026-04-15T10:00:00+00:00"`}
            </pre>
          )}

          {active === 'redaction-proof.json' && ioFile === null && (
            <pre className="whitespace-pre text-[var(--color-text-muted)]">
{`{
  "schema_version": "0.1.0",
  "scanner": "novafabric.redact@0.6.12",
  "scanned_at": "2026-04-15T10:00:04+00:00",
  "findings": [],
  "rules_applied": [
    "openai.api_key",
    "anthropic.api_key",
    "github.token",
    "aws.access_key",
    "generic.password",
    "generic.bearer_token"
  ],
  "redaction_strategy": "replace-with-placeholder",
  "chain_hash": "a3f8c4e9b1d6f2a7c5e8b1d4f9a2c7e0b3d6f9a2c5e8b1d4f7a0c3e6b9d2f5"
}`}
            </pre>
          )}

          {ioFile !== null && (
            ioLoading
              ? <p className="text-[var(--color-text-faint)] text-xs">loading…</p>
              : <pre className="whitespace-pre-wrap break-all text-[var(--color-text-muted)]">{ioContent ?? ''}</pre>
          )}
          </RenderErrorBoundary>
        </div>

        {validation && validation.errors && validation.errors.length > 0 && (
          <footer className="border-t border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 max-h-40 overflow-auto">
            <h4 className="text-xs font-medium text-[var(--color-status-failure)] mb-2">Validation errors</h4>
            <ul className="space-y-1 text-xs font-mono text-[var(--color-text-muted)]">
              {validation.errors.slice(0, 10).map((e, i) => (
                <li key={i}>
                  <span className="text-[var(--color-status-failure)]">{e.path || '/'}</span> · {e.keyword}: {e.message}
                </li>
              ))}
            </ul>
          </footer>
        )}
      </section>
    </div>
  );
}

function TraceSpanView({
  spans,
  hoveredSpan,
  onHover,
}: {
  spans: unknown[];
  hoveredSpan: string | null;
  onHover: (id: string | null) => void;
}) {
  return (
    <ul className="space-y-3">
      {spans.map((span, i) => {
        const s = span as Record<string, unknown>;
        const spanId = (s.span_id as string) ?? '';
        const name = (s.name as string) ?? '';
        const status = (s.status as string) ?? '';
        const durationMs = s.duration_ms as number | undefined;
        const startedAt = s.started_at as string | undefined;
        const finishedAt = s.finished_at as string | undefined;
        const parentSpanId = s.parent_span_id as string | null;
        const attributes = s.attributes as Record<string, unknown> | null | undefined;
        const matched = hoveredSpan === spanId;
        const ok = status === 'ok' || status === 'success';

        return (
          <li
            key={i}
            onMouseEnter={() => onHover(spanId)}
            onMouseLeave={() => onHover(null)}
            className={clsx(
              'rounded-lg border p-4 transition-colors cursor-default',
              matched
                ? 'border-[var(--color-accent)]/40 bg-[var(--color-bg-raised)]'
                : 'border-[var(--color-border)] bg-[var(--color-bg-raised)]/50 hover:bg-[var(--color-bg-raised)]',
            )}
          >
            <header className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 min-w-0">
                <code className="text-[var(--color-text)] font-medium truncate">{name}</code>
                {durationMs != null && (
                  <span className="text-xs text-[var(--color-text-faint)] shrink-0">{durationMs} ms</span>
                )}
                {status && (
                  <span className={clsx(
                    'shrink-0 text-[9px] uppercase tracking-wider px-1 py-px rounded font-medium',
                    ok
                      ? 'bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)] text-[var(--color-status-success)]'
                      : 'bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)] text-[var(--color-status-failure)]',
                  )}>
                    {status}
                  </span>
                )}
              </div>
              <code className="text-[10px] text-[var(--color-text-faint)] shrink-0 ml-3">{spanId}</code>
            </header>

            <dl className="text-xs text-[var(--color-text-muted)] space-y-1.5">
              {parentSpanId && (
                <div className="flex justify-between gap-4">
                  <span className="shrink-0">parent_span_id</span>
                  <code className="text-[var(--color-text-faint)] truncate text-right">{parentSpanId}</code>
                </div>
              )}
              {startedAt && (
                <div className="flex justify-between gap-4">
                  <span className="shrink-0">started_at</span>
                  <code className="text-[var(--color-text)] text-right">{startedAt}</code>
                </div>
              )}
              {finishedAt && (
                <div className="flex justify-between gap-4">
                  <span className="shrink-0">finished_at</span>
                  <code className="text-[var(--color-text)] text-right">{finishedAt}</code>
                </div>
              )}
            </dl>

            {attributes && Object.keys(attributes).length > 0 && (
              <div className="mt-3 pt-3 border-t border-[var(--color-border)]">
                <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-2">attributes</p>
                <dl className="space-y-1.5">
                  {Object.entries(attributes).map(([k, v]) => (
                    <div key={k} className="flex gap-4 text-xs">
                      <code className="shrink-0 text-[var(--color-text-faint)]">{k}</code>
                      <code className="text-[var(--color-text)] break-all">
                        {Array.isArray(v)
                          ? JSON.stringify(v)
                          : typeof v === 'object' && v !== null
                            ? <pre className="whitespace-pre-wrap">{JSON.stringify(v, null, 2)}</pre>
                            : String(v)}
                      </code>
                    </div>
                  ))}
                </dl>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ModelCallsView({ calls, hoveredSpan }: { calls: unknown[]; hoveredSpan: string | null }) {
  return (
    <ul className="space-y-3">
      {calls.map((call, i) => {
        const c = normalizeModelCall(call as Record<string, unknown>);
        const matched = hoveredSpan?.startsWith('model_') && hoveredSpan.endsWith(String(i + 1).padStart(3, '0'));
        const ok = c.status === 'success' || c.status === 'ok';
        return (
          <li
            key={c.id || i}
            className={clsx(
              'rounded-lg border p-4 transition-colors',
              matched
                ? 'border-[var(--color-accent)]/40 bg-[var(--color-bg-raised)]'
                : 'border-[var(--color-border)] bg-[var(--color-bg-raised)]/50',
            )}
          >
            <header className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <code className="text-[var(--color-text)] font-medium">
                  {c.system ? `${c.system}.${c.model}` : c.model}
                </code>
                <span className="text-xs text-[var(--color-text-faint)]">{c.durationMs} ms</span>
                <span className={clsx(
                  'text-[9px] uppercase tracking-wider px-1 py-px rounded font-medium',
                  ok
                    ? 'bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)] text-[var(--color-status-success)]'
                    : 'bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)] text-[var(--color-status-failure)]',
                )}>
                  {c.status}
                </span>
              </div>
              <code className="text-xs text-[var(--color-text-faint)]">{c.id}</code>
            </header>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs text-[var(--color-text-muted)]">
              {c.temperature != null && (
                <div className="flex justify-between">
                  <span>temperature</span><span className="text-[var(--color-text)]">{c.temperature}</span>
                </div>
              )}
              {c.maxTokens != null && (
                <div className="flex justify-between">
                  <span>max_tokens</span><span className="text-[var(--color-text)]">{c.maxTokens}</span>
                </div>
              )}
              {c.finishReason && (
                <div className="flex justify-between">
                  <span>finish_reason</span><span className="text-[var(--color-text)]">{c.finishReason}</span>
                </div>
              )}
              {(c.inputTokens != null || c.outputTokens != null) && (
                <div className="flex justify-between">
                  <span>tokens (in/out)</span>
                  <span className="text-[var(--color-text)]">{c.inputTokens ?? '?'} / {c.outputTokens ?? '?'}</span>
                </div>
              )}
              {c.messages.length > 0 && (
                <div className="flex justify-between">
                  <span>messages</span><span className="text-[var(--color-text)]">{c.messages.length}</span>
                </div>
              )}
              {c.agent && (
                <div className="flex justify-between">
                  <span>agent</span><span className="text-[var(--color-text)]">{c.agent}</span>
                </div>
              )}
              {c.phase && (
                <div className="flex justify-between">
                  <span>phase</span><span className="text-[var(--color-text)]">{c.phase}</span>
                </div>
              )}
            </dl>
            {c.cacheHit != null && (
              <div className="mt-2 pt-2 border-t border-[var(--color-border)]">
                <span className={clsx(
                  'px-1.5 py-0.5 rounded text-[10px] font-medium',
                  c.cacheHit
                    ? 'bg-[color-mix(in_oklab,#10b981_15%,transparent)] text-[#10b981]'
                    : 'bg-[var(--color-bg-sunken)] text-[var(--color-text-faint)]',
                )}>
                  {c.cacheHit ? 'cache hit' : 'no cache'}{c.cacheKind ? ` (${c.cacheKind})` : ''}
                </span>
              </div>
            )}
            {c.completionPreview && (
              <div className="mt-3 pt-3 border-t border-[var(--color-border)] text-xs">
                <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">completion preview</p>
                <p className="text-[var(--color-text-muted)] italic">
                  &quot;{c.completionPreview.slice(0, 200)}{c.completionPreview.length > 200 ? '…' : ''}&quot;
                </p>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ToolCallsView({ calls, hoveredSpan }: { calls: unknown[]; hoveredSpan: string | null }) {
  return (
    <ul className="space-y-3">
      {calls.map((call, i) => {
        const c = normalizeToolCall(call as Record<string, unknown>);
        const matched = hoveredSpan === `tool_${String(i + 1).padStart(3, '0')}`;
        const ok = (c.status === 'success' || c.status === 'ok') && !c.resultError;
        return (
          <li
            key={c.id || i}
            className={clsx(
              'rounded-lg border p-4',
              matched ? 'border-[var(--color-accent)]/40' : 'border-[var(--color-border)]',
              'bg-[var(--color-bg-raised)]/50',
            )}
          >
            <header className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <code className="text-[var(--color-text)] font-medium">
                  {c.toolName}{c.toolVersion ? `@${c.toolVersion}` : ''}
                </code>
                {c.transport && (
                  <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] px-1.5 py-0.5 border border-[var(--color-border)] rounded">{c.transport}</span>
                )}
                {c.mutates && (
                  <span className="text-[10px] uppercase tracking-wider text-[var(--color-status-pending)] px-1.5 py-0.5 border border-[var(--color-status-pending)]/30 rounded">mutating</span>
                )}
              </div>
              <span className="text-xs text-[var(--color-text-faint)]">{c.durationMs} ms</span>
            </header>
            <dl className="text-xs text-[var(--color-text-muted)] space-y-1">
              {c.arguments && (
                <div className="flex flex-col gap-1">
                  <span>args</span>
                  <pre className="text-[var(--color-text)] whitespace-pre-wrap break-all bg-[var(--color-bg-sunken)] rounded px-2 py-1.5 text-[11px]">
                    {typeof c.arguments === 'object'
                      ? JSON.stringify(c.arguments, null, 2)
                      : String(c.arguments)}
                  </pre>
                </div>
              )}
              <div className="flex justify-between">
                <span>status</span>
                <code className={ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]'}>
                  {c.status}
                </code>
              </div>
              {c.resultText && (
                <div className="flex justify-between gap-4">
                  <span className="shrink-0">result</span>
                  <code className="text-[var(--color-text)] truncate max-w-[60%] text-right">{c.resultText}</code>
                </div>
              )}
              {c.agent && (
                <div className="flex justify-between">
                  <span>agent</span><code className="text-[var(--color-text)]">{c.agent}</code>
                </div>
              )}
              {c.mcpServerName && (
                <div className="flex justify-between">
                  <span>mcp server</span><code className="text-[var(--color-text)]">{c.mcpServerName}</code>
                </div>
              )}
            </dl>
          </li>
        );
      })}
    </ul>
  );
}
