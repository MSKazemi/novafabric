import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { api } from '../../../lib/api';
import type { AssetSummary, LineageEdgePayload, LineageRecord, OpenLineageExportResult } from '../../../lib/api';
import type { AssetRecord, LineageEdgeRecord } from '../../../lib/fixtures';
import type { LineageNode } from '../../../lib/lineage';
import LineageGraph from '../../lineage/LineageGraph';
import { ErrorBox, Loading } from '../helpers';
import { SuggestInput } from '../../ui/SuggestInput';
import CopyButton from '../../ui/CopyButton';

const LIMIT_OPTIONS = [
  { label: '50', value: 50 },
  { label: '100', value: 100 },
  { label: '200', value: 200 },
  { label: '500', value: 500 },
  { label: 'all', value: 5000 },
];

type QueryMode = 'provenance' | 'blast-radius' | 'replay-chain' | 'time-travel';

interface TimeTravelResult {
  mode: 'time-travel';
  ref: string;
  records: LineageRecord[];
  label: string;
  supported: boolean;
}

interface StandardResult {
  mode: Exclude<QueryMode, 'time-travel'>;
  ref: string;
  records: LineageRecord[];
  label: string;
}

type QueryResult = StandardResult | TimeTravelResult;

function QueryPanel({ onClose, assets, runIds, edgeTypeFilter }: { onClose: () => void; assets: AssetSummary[]; runIds: string[]; edgeTypeFilter?: string }) {
  const [mode, setMode] = useState<QueryMode>('provenance');
  const [ref, setRef] = useState('');
  const [depth, setDepth] = useState('5');
  const [at, setAt] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<QueryResult | null>(null);

  const suggestions = mode === 'replay-chain' || mode === 'time-travel'
    ? runIds
    : assets.map(a => `${a.name}@${a.version}`);

  const run = useCallback(async () => {
    const trimmed = ref.trim();
    if (!trimmed) return;
    if (mode === 'time-travel' && !at) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      if (mode === 'provenance') {
        const r = await api.provenance(trimmed, Number(depth) || 5, edgeTypeFilter);
        setResult({ mode, ref: trimmed, records: r.ancestors, label: `${r.ancestors.length} ancestor${r.ancestors.length !== 1 ? 's' : ''}` });
      } else if (mode === 'blast-radius') {
        const r = await api.blastRadius(trimmed, Number(depth) || 5, edgeTypeFilter);
        setResult({ mode, ref: trimmed, records: r.descendants, label: `${r.descendants.length} descendant${r.descendants.length !== 1 ? 's' : ''}` });
      } else if (mode === 'replay-chain') {
        const r = await api.replayChain(trimmed);
        setResult({ mode, ref: trimmed, records: r.chain, label: `${r.chain.length} replay${r.chain.length !== 1 ? 's' : ''}` });
      } else {
        // time-travel
        const atIso = at.includes('T') ? at : `${at}T00:00:00Z`;
        const r = await api.timeTravelLineage(trimmed, atIso, Number(depth) || 5);
        setResult({
          mode: 'time-travel',
          ref: trimmed,
          records: r.ancestors,
          label: `${r.ancestors.length} node${r.ancestors.length !== 1 ? 's' : ''} at ${at}`,
          supported: r.supported,
        });
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [mode, ref, depth, at, edgeTypeFilter]);

  const modeLabel: Record<QueryMode, string> = {
    'provenance': 'Provenance (ancestors)',
    'blast-radius': 'Blast Radius (descendants)',
    'replay-chain': 'Replay Chain',
    'time-travel': 'Time-Travel',
  };

  const refPlaceholder: Record<QueryMode, string> = {
    'provenance': 'asset-name@version or run_id',
    'blast-radius': 'asset-name@version or node_id',
    'replay-chain': 'original run_id',
    'time-travel': 'asset-name@version or run_id',
  };

  const cliEquiv: Record<QueryMode, string> = {
    'provenance': `nova lineage provenance ${ref || '<ref>'} --depth ${depth}`,
    'blast-radius': `nova lineage blast-radius ${ref || '<ref>'} --depth ${depth}`,
    'replay-chain': `nova lineage replay-chain ${ref || '<run_id>'}`,
    'time-travel': `nova lineage time-travel ${ref || '<ref>'} --at ${at || '<ISO-datetime>'}`,
  };

  const isTimeTravelQueryDisabled = loading || !ref.trim() || (mode === 'time-travel' && !at);

  return (
    <div className="rounded-lg border border-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_4%,var(--color-bg-raised))] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Lineage Query</h3>
        <button
          onClick={onClose}
          className="text-[10px] text-[var(--color-text-faint)] hover:text-[var(--color-text)] px-2 py-0.5 rounded border border-[var(--color-border)] transition-colors"
        >
          close
        </button>
      </div>

      {/* Mode select */}
      <div className="flex gap-1 flex-wrap">
        {(['provenance', 'blast-radius', 'replay-chain', 'time-travel'] as QueryMode[]).map((m) => (
          <button
            key={m}
            onClick={() => { setMode(m); setResult(null); setError(null); }}
            className={[
              'px-2.5 py-1 text-[10px] rounded border transition-colors',
              mode === m
                ? 'border-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_15%,transparent)] text-[var(--color-accent)]'
                : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent)]',
            ].join(' ')}
          >
            {modeLabel[m]}
          </button>
        ))}
      </div>

      {/* Input row */}
      <div className="flex gap-2">
        <SuggestInput
          value={ref}
          onChange={v => setRef(v)}
          suggestions={suggestions}
          onEnter={run}
          placeholder={refPlaceholder[mode]}
          className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        {mode !== 'replay-chain' && mode !== 'time-travel' && (
          <input
            type="number"
            min={1}
            max={20}
            value={depth}
            onChange={e => setDepth(e.target.value)}
            title="Max depth"
            className="w-16 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-center focus:border-[var(--color-accent)] focus:outline-none"
          />
        )}
        <button
          onClick={run}
          disabled={isTimeTravelQueryDisabled}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {loading ? '…' : 'Query'}
        </button>
      </div>

      {/* Time-travel: datetime-local input */}
      {mode === 'time-travel' && (
        <div className="flex items-center gap-2">
          <label className="text-[10px] text-[var(--color-text-faint)] shrink-0">At:</label>
          <input
            type="datetime-local"
            value={at}
            onChange={e => setAt(e.target.value)}
            className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
          <input
            type="number"
            min={1}
            max={20}
            value={depth}
            onChange={e => setDepth(e.target.value)}
            title="Max depth"
            className="w-16 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-center focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
      )}

      {/* CLI equivalent */}
      <div className="font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)]">
        $ {cliEquiv[mode]}
      </div>

      {/* Error */}
      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {/* Results */}
      {result && (
        <div className="space-y-1.5">
          {/* Time-travel unsupported notice */}
          {result.mode === 'time-travel' && !result.supported && (
            <div className="text-xs text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-pending)_25%,transparent)] rounded px-3 py-2">
              Time-travel not supported by current backend (requires Postgres or KuzuDB)
            </div>
          )}
          {(result.mode !== 'time-travel' || result.supported) && (
            <>
              <div className="text-[10px] text-[var(--color-text-faint)]">
                {result.label} for <span className="font-mono text-[var(--color-text)]">{result.ref}</span>
              </div>
              {result.records.length === 0 ? (
                <div className="text-xs text-[var(--color-text-faint)] italic px-2 py-3 text-center">
                  No {mode === 'provenance' ? 'ancestors' : mode === 'blast-radius' ? 'descendants' : mode === 'time-travel' ? 'nodes' : 'replays'} found.
                </div>
              ) : (
                <div className="max-h-48 overflow-y-auto space-y-1 pr-1">
                  {result.records.map((rec, i) => (
                    <div key={i} className="flex items-start gap-2 text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5">
                      <span className="shrink-0 font-mono text-[var(--color-text-faint)] w-4 text-right">{i + 1}</span>
                      <div className="min-w-0 flex-1 space-y-px">
                        <div className="font-mono text-[var(--color-text)] truncate">{rec.ref || rec.node_id}</div>
                        {rec.kind && (
                          <div className="text-[var(--color-text-faint)]">{rec.kind}</div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── LineageImportPanel ────────────────────────────────────────────────────────

function LineageImportPanel({ runIds }: { runIds: string[] }) {
  const [capsulePath, setCapsulePath] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; imported: number; skipped: number; file_count: number; note: string } | null>(null);

  const run = useCallback(async () => {
    const path = capsulePath.trim();
    if (!path) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.lineageImport(path);
      setResult(res);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [capsulePath]);

  const cliCmd = `nova lineage import ${capsulePath || '<capsule_path>'}`;
  const inputClass =
    'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Import Lineage from Capsule</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Import OpenLineage events from a capsule directory into the lineage store.
          </p>
        </div>
      </div>

      <div className="space-y-1">
        <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          Capsule path or run ID
        </label>
        <SuggestInput
          value={capsulePath}
          onChange={setCapsulePath}
          suggestions={runIds}
          placeholder="run_2024_... or /absolute/path/to/capsule"
          className={inputClass + ' w-full'}
          onEnter={run}
        />
      </div>

      <button
        onClick={run}
        disabled={loading || !capsulePath.trim()}
        className={[
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !capsulePath.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        ].join(' ')}
      >
        {loading ? 'importing…' : 'Import'}
      </button>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className={[
            'text-xs font-mono font-bold px-3 py-2 rounded border',
            result.ok
              ? 'text-[var(--color-status-success)] border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
              : 'text-[var(--color-status-failure)] border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
          ].join(' ')}>
            {result.ok ? 'Import successful' : 'Import failed'}
          </div>
          {result.ok && (
            <div className="grid grid-cols-3 gap-2 text-[10px] font-mono">
              <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 space-y-0.5">
                <div className="text-[var(--color-text-faint)] uppercase tracking-wider">Imported</div>
                <div className="text-[var(--color-text)] font-bold">{result.imported}</div>
              </div>
              <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 space-y-0.5">
                <div className="text-[var(--color-text-faint)] uppercase tracking-wider">Skipped</div>
                <div className="text-[var(--color-text)] font-bold">{result.skipped}</div>
              </div>
              <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 space-y-0.5">
                <div className="text-[var(--color-text-faint)] uppercase tracking-wider">Capsules</div>
                <div className="text-[var(--color-text)] font-bold">{result.file_count}</div>
              </div>
            </div>
          )}
          <p className="text-[10px] text-[var(--color-text-faint)]">{result.note}</p>
        </div>
      )}

      {/* CLI equivalent */}
      <div className="space-y-1">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">CLI equivalent</p>
        <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <pre className="text-[10px] font-mono text-[var(--color-text-muted)]">$ {cliCmd}</pre>
          <div className="absolute top-1.5 right-1.5">
            <CopyButton text={cliCmd} label="CLI" />
          </div>
        </div>
      </div>
    </section>
  );
}

// ── OpenLineageExportPanel — mirrors `nova lineage emit-openlineage` ──────────

function OpenLineageExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<OpenLineageExportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const copiedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current); }, []);

  const emit = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.lineageEmitOpenLineage(id);
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [runId]);

  const copyJson = () => {
    if (!result) return;
    navigator.clipboard.writeText(JSON.stringify(result.events, null, 2)).catch(() => {});
    if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current);
    setCopied(true);
    copiedTimerRef.current = setTimeout(() => setCopied(false), 1500);
  };

  const inputClass =
    'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full';

  const cliCmd = runId.trim()
    ? `nova lineage emit-openlineage ${runId.trim()}`
    : 'nova lineage emit-openlineage <capsule_dir>';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">Export OpenLineage Events</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          Build OpenLineage-compatible job/run events from a capsule and return them as JSON.
        </p>
      </div>

      <div className="space-y-1">
        <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          Run ID
        </label>
        <SuggestInput
          value={runId}
          onChange={setRunId}
          suggestions={runIds}
          placeholder="01KRK8..."
          className={inputClass}
          onEnter={emit}
        />
      </div>

      <button
        onClick={emit}
        disabled={loading || !runId.trim()}
        className={[
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !runId.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        ].join(' ')}
      >
        {loading ? 'building…' : 'Export events'}
      </button>

      {/* CLI reference */}
      <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
        <pre className="text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap">{cliCmd}</pre>
        <div className="absolute top-1.5 right-1.5">
          <CopyButton text={cliCmd} label="CLI" />
        </div>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className={[
            'flex items-center justify-between text-xs font-mono font-bold px-3 py-2 rounded border',
            result.ok
              ? 'text-[var(--color-status-success)] border-[color-mix(in_oklab,var(--color-status-success)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
              : 'text-[var(--color-status-failure)] border-[color-mix(in_oklab,var(--color-status-failure)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)]',
          ].join(' ')}>
            <span>{result.ok ? `✓ ${result.event_count} event${result.event_count !== 1 ? 's' : ''}` : `✗ ${result.error ?? 'failed'}`}</span>
            {result.ok && result.events.length > 0 && (
              <button
                onClick={copyJson}
                className="text-[10px] font-mono px-2 py-px rounded border border-current opacity-70 hover:opacity-100 transition-opacity"
              >
                {copied ? 'copied!' : 'copy JSON'}
              </button>
            )}
          </div>
          {result.ok && result.events.length > 0 && (
            <pre className="text-[10px] font-mono text-[var(--color-text-muted)] bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] px-3 py-2 max-h-64 overflow-auto whitespace-pre-wrap">
              {JSON.stringify(result.events.slice(0, 3), null, 2)}
              {result.events.length > 3 && `\n… and ${result.events.length - 3} more events`}
            </pre>
          )}
        </div>
      )}
    </section>
  );
}

// ── ProvJsonExportPanel ───────────────────────────────────────────────────────

function ProvJsonExportPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; run_id: string; document: object; note: string } | null>(null);

  function handleExport() {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    api.exportProvJson(id)
      .then(r => setResult(r))
      .catch(e => setError(e.message ?? String(e)))
      .finally(() => setLoading(false));
  }

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">PROV-JSON Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Export W3C PROV-JSON lineage document — mirrors <code className="font-mono">nova lineage export-prov</code>
          </p>
        </div>
      </div>

      <div className="flex gap-2 items-end">
        <div className="flex-1">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="run_id"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none w-full"
          />
        </div>
        <button
          type="button"
          onClick={handleExport}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap"
        >
          {loading ? 'Exporting…' : 'Export PROV-JSON'}
        </button>
      </div>

      <p className="text-[10px] text-[var(--color-text-faint)]">
        W3C PROV-JSON — requires <code className="font-mono">lineage.jsonl</code> in the capsule
      </p>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">run_id: <span className="font-mono">{result.run_id}</span></span>
            <CopyButton text={JSON.stringify(result.document, null, 2)} />
          </div>
          <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded p-2 overflow-auto max-h-64 whitespace-pre-wrap break-all">
            {JSON.stringify(result.document, null, 2)}
          </pre>
          {result.note && (
            <p className="text-[10px] text-[var(--color-text-faint)]">{result.note}</p>
          )}
        </div>
      )}
    </section>
  );
}

export default function LineageTab({ refreshTick, onCountChange }: { refreshTick: number; onCountChange?: (n: number) => void }) {
  const [edges, setEdges] = useState<LineageEdgePayload[] | null>(null);
  const [assets, setAssets] = useState<AssetSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(100);
  const [edgeTypeFilter, setEdgeTypeFilter] = useState('all');
  const [nodeSearch, setNodeSearch] = useState('');
  const [showQuery, setShowQuery] = useState(false);
  const [selectedNode, setSelectedNode] = useState<LineageNode | null>(null);

  // Keep a ref so refresh never re-creates just because the parent re-renders
  const onCountChangeRef = useRef(onCountChange);
  useEffect(() => { onCountChangeRef.current = onCountChange; }, [onCountChange]);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [e, a] = await Promise.all([api.edges(limit), api.listAssets()]);
      setEdges(e.edges);
      setAssets(a.assets);
      onCountChangeRef.current?.(e.count);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [limit]); // onCountChange intentionally excluded — accessed via ref

  useEffect(() => { refresh(); }, [refresh, refreshTick]);

  const ancestors = useMemo<LineageNode[]>(() => {
    if (!selectedNode || !edges) return [];
    const parentMap = new Map<string, string>();
    for (const e of edges) parentMap.set(e.target, e.source);
    const chain: string[] = [];
    let cur = parentMap.get(selectedNode.id);
    const visited = new Set<string>();
    while (cur && !visited.has(cur)) { visited.add(cur); chain.unshift(cur); cur = parentMap.get(cur); }
    if (chain.length === 0) return [];
    const nodeById = new Map<string, LineageNode>();
    for (const e of edges) {
      if (!nodeById.has(e.source)) nodeById.set(e.source, { id: e.source, data: { kind: 'asset', ref: e.source, label: e.source }, position: { x: 0, y: 0 }, type: 'lineage' } as LineageNode);
      if (!nodeById.has(e.target)) nodeById.set(e.target, { id: e.target, data: { kind: 'asset', ref: e.target, label: e.target }, position: { x: 0, y: 0 }, type: 'lineage' } as LineageNode);
    }
    return chain.map(id => nodeById.get(id) ?? ({ id, data: { kind: 'asset', ref: id, label: id }, position: { x: 0, y: 0 }, type: 'lineage' } as LineageNode));
  }, [selectedNode, edges]);

  if (error && !edges) return <ErrorBox message={error} onRetry={refresh} />;
  if (!edges || !assets) return <Loading />;

  const edgeTypes = [...new Set(edges.map(e => e.edge_type))].sort();

  const adaptedEdges: LineageEdgeRecord[] = edges.map((e) => ({
    edge_id: e.edge_id,
    edge_type: e.edge_type as LineageEdgeRecord['edge_type'],
    source: e.source,
    target: e.target,
    capsule_run_id: e.capsule_run_id,
    confidence: e.confidence,
    created_at: e.created_at,
    direction: e.direction,
  }));

  const adaptedAssets: AssetRecord[] = assets.map((a) => ({
    name: a.name,
    version: a.version,
    asset_type: a.asset_type as AssetRecord['asset_type'],
    status: (a.status === 'production' || a.status === 'promoted' ? 'promoted' : 'development'),
    description: '',
    spec: {},
  }));

  const filteredEdges = adaptedEdges.filter(e => {
    if (edgeTypeFilter !== 'all' && e.edge_type !== edgeTypeFilter) return false;
    const q = nodeSearch.trim().toLowerCase();
    if (!q) return true;
    return e.source.toLowerCase().includes(q) || e.target.toLowerCase().includes(q);
  });


  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <p className="text-xs text-[var(--color-text-muted)]">
            {filteredEdges.length}{filteredEdges.length !== adaptedEdges.length ? ` / ${adaptedEdges.length}` : ''} edge{filteredEdges.length === 1 ? '' : 's'}
          </p>
          <button
            onClick={() => setShowQuery(q => !q)}
            className={[
              'text-[10px] px-2 py-0.5 rounded border transition-colors',
              showQuery
                ? 'border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)]'
                : 'border-[var(--color-border)] text-[var(--color-text-faint)] hover:text-[var(--color-text-muted)] hover:border-[var(--color-accent)]',
            ].join(' ')}
          >
            {showQuery ? '▴ hide query' : '⌕ query provenance / blast-radius / replay-chain / time-travel'}
          </button>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1 text-xs text-[var(--color-text-muted)]">
            Show:
            <select
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded px-1 py-0.5 text-xs text-[var(--color-text)] cursor-pointer"
            >
              {LIMIT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
            edges
          </label>
          <button onClick={refresh} className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)]">
            refresh
          </button>
        </div>
      </div>

      {/* Lineage query panel */}
      {showQuery && (
        <QueryPanel
          onClose={() => setShowQuery(false)}
          assets={assets}
          runIds={[...new Set(edges.map(e => e.capsule_run_id).filter(Boolean))]}
          edgeTypeFilter={edgeTypeFilter}
        />
      )}

      {/* Filter row */}
      <div className="flex items-center gap-2">
        <input
          type="search"
          value={nodeSearch}
          onChange={e => setNodeSearch(e.target.value)}
          placeholder="Search node name…"
          className="flex-1 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <select
          value={edgeTypeFilter}
          onChange={e => setEdgeTypeFilter(e.target.value)}
          className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-1.5 py-1.5 font-mono text-xs"
        >
          <option value="all">all types</option>
          {edgeTypes.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>

      {selectedNode && ancestors.length > 0 && (
        <nav className="flex items-center gap-1 text-[10px] text-[var(--color-text-faint)] mb-2 flex-wrap">
          {ancestors.map((n, i) => (
            <span key={n.id} className="flex items-center gap-1">
              {i > 0 && <span>→</span>}
              <button
                type="button"
                onClick={() => setSelectedNode(n)}
                className="hover:text-[var(--color-text)] hover:underline transition-colors"
              >{n.data.label ?? n.id}</button>
            </span>
          ))}
          <span>→</span>
          <span className="text-[var(--color-text)] font-medium">{selectedNode.data.label ?? selectedNode.id}</span>
        </nav>
      )}
      <LineageGraph
        edges={filteredEdges}
        assets={adaptedAssets}
        initialSelectedNodeId={null}
        initialMode="all"
        emptyMessage="No lineage edges yet. Run captures that consume registered assets to build the graph."
        onNodeSelect={setSelectedNode}
      />

      {/* Lineage import panel */}
      <LineageImportPanel
        runIds={[...new Set(edges.map(e => e.capsule_run_id).filter(Boolean))]}
      />

      {/* OpenLineage export panel */}
      <OpenLineageExportPanel
        runIds={[...new Set(edges.map(e => e.capsule_run_id).filter(Boolean))]}
      />

      {/* PROV-JSON export panel */}
      <ProvJsonExportPanel
        runIds={[...new Set(edges.map(e => e.capsule_run_id).filter(Boolean))]}
      />
    </div>
  );
}
