/**
 * v0.46.0 CLI-parity panels rendered below the runs grid: distributed capsule
 * tree (`nova run show --with-children`), spool lineage edges
 * (`nova run lineage`), and secret scan (`nova scan-secrets`).
 * Extracted verbatim from the former RunsTab monolith — behavior frozen.
 */
import { useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import type { CapsuleTreeNode } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import { SEVERITY_STYLE, SeverityBadge } from './severity';

function TreeNodeRow({ node, depth }: { node: CapsuleTreeNode; depth: number }) {
  return (
    <>
      <div className="flex items-center gap-2 text-xs font-mono py-0.5 min-w-0" style={{ paddingLeft: depth * 16 }}>
        <span className="shrink-0 text-[var(--text-2xs)] uppercase text-[var(--color-accent)] px-1.5 py-px rounded bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] border border-[color-mix(in_oklab,var(--color-accent)_30%,transparent)]">
          {node.capsule_role}
        </span>
        <code className="text-[var(--color-text)] truncate">{node.run_id}</code>
        <span className="shrink-0 text-[10px] text-[var(--color-text-faint)]">{node.status}</span>
        {node.is_synthetic && (
          <span className="shrink-0 text-[var(--text-2xs)] text-[var(--color-text-faint)] italic">(synthetic)</span>
        )}
      </div>
      {node.children.map(c => <TreeNodeRow key={c.run_id} node={c} depth={depth + 1} />)}
    </>
  );
}

export function CapsuleTreePanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [data, setData] = useState<{ root: CapsuleTreeNode; total_nodes: number; orphans: CapsuleTreeNode[] } | null>(null);

  async function load() {
    if (!runId.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setData(null);
    try {
      const r = await api.runTree(runId.trim());
      setData(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <header>
        <h3 className="text-xs font-medium text-[var(--color-text)]">Distributed Capsule Tree</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] font-mono mt-0.5">
          Parent/child capsule hierarchy — <code>nova run show --with-children</code>
        </p>
      </header>
      <div className="flex gap-2 items-start flex-wrap">
        <div className="flex-1 min-w-[220px] max-w-md">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            onEnter={load}
            placeholder="run_id"
            className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={load}
          disabled={busy || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded font-medium bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-60 transition-colors"
        >
          {busy ? 'Loading…' : 'Load tree'}
        </button>
      </div>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {data && (
        <div className="space-y-2">
          <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            {data.total_nodes} node{data.total_nodes !== 1 ? 's' : ''}
          </p>
          <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 overflow-x-auto">
            <TreeNodeRow node={data.root} depth={0} />
          </div>
          {data.orphans.length > 0 && (
            <div className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)] p-3">
              <h4 className="text-[10px] uppercase tracking-wider text-[var(--color-status-pending)] font-mono mb-1">
                Orphans ({data.orphans.length})
              </h4>
              {data.orphans.map(o => <TreeNodeRow key={o.run_id} node={o} depth={0} />)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const SPOOL_EDGE_TYPES = ['contains', 'spawned', 'delegated_to', 'replayed_from'] as const;

export function RunSpoolLineagePanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [edgeTypes, setEdgeTypes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [data, setData] = useState<{ edges: Array<Record<string, unknown>>; count: number } | null>(null);

  async function query() {
    if (!runId.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setData(null);
    try {
      const r = await api.runSpoolLineage(runId.trim(), edgeTypes.length > 0 ? edgeTypes.join(',') : undefined);
      setData(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <header>
        <h3 className="text-xs font-medium text-[var(--color-text)]">Run Lineage Edges</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] font-mono mt-0.5">
          Spool lineage edges with edge-type filter — <code>nova run lineage</code>
        </p>
      </header>
      <div className="flex gap-2 items-start flex-wrap">
        <div className="flex-1 min-w-[220px] max-w-md">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            onEnter={query}
            placeholder="run_id"
            className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={query}
          disabled={busy || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded font-medium bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-60 transition-colors"
        >
          {busy ? 'Querying…' : 'Query'}
        </button>
      </div>
      {/* Edge-type filter — none checked = no filter */}
      <div className="flex items-center gap-3 flex-wrap text-[10px] font-mono text-[var(--color-text-muted)]">
        {SPOOL_EDGE_TYPES.map(et => (
          <label key={et} className="flex items-center gap-1.5 cursor-pointer">
            <input
              type="checkbox"
              checked={edgeTypes.includes(et)}
              onChange={() =>
                setEdgeTypes(prev => prev.includes(et) ? prev.filter(x => x !== et) : [...prev, et])
              }
              className="w-3 h-3 rounded border border-[var(--color-border)] accent-[var(--color-accent)] cursor-pointer"
            />
            {et}
          </label>
        ))}
        <span className="text-[var(--color-text-faint)]">(none checked = no filter)</span>
      </div>
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      {data && (
        <div className="space-y-2">
          <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            {data.count} edge{data.count !== 1 ? 's' : ''}
          </p>
          {data.edges.length === 0 ? (
            <p className="text-xs text-[var(--color-text-muted)]">No lineage edges recorded for this run.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono text-[var(--color-text-muted)]">
                <thead>
                  <tr className="text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                    <th className="text-left pb-1.5 pr-3">source_run_id</th>
                    <th className="text-left pb-1.5 pr-3">edge_type</th>
                    <th className="text-left pb-1.5">target_run_id</th>
                  </tr>
                </thead>
                <tbody>
                  {data.edges.map((e, i) => (
                    <tr key={i} className="border-b border-[var(--color-border)] last:border-0">
                      <td className="py-1.5 pr-3 break-all">{String(e.source_run_id ?? '—')}</td>
                      <td className="py-1.5 pr-3">
                        <span className="text-[var(--text-2xs)] px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
                          {String(e.edge_type ?? '—')}
                        </span>
                      </td>
                      <td className="py-1.5 break-all">{String(e.target_run_id ?? '—')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface ScanSecretsResult {
  ok: boolean;
  run_id: string;
  findings_count: { total?: number; by_severity?: Record<string, number> };
  findings: Array<{ rule_id: string; severity: string; target_ref: string; redaction_strategy: string }>;
  fail_on: string | null;
  triggered: boolean;
  triggered_count: number;
}

export function ScanSecretsPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [failOn, setFailOn] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [data, setData] = useState<ScanSecretsResult | null>(null);

  async function scan() {
    if (!runId.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setData(null);
    try {
      const r = await api.scanSecrets(runId.trim(), failOn || undefined);
      setData(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const notFound = err !== null && (err.includes('404') || err.toLowerCase().includes('not found'));
  const total = data?.findings_count.total ?? data?.findings.length ?? 0;
  const bySev = data?.findings_count.by_severity ?? {};

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <header>
        <h3 className="text-xs font-medium text-[var(--color-text)]">Secret Scan</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] font-mono mt-0.5">
          Secrets/PII findings from the redaction log — <code>nova scan-secrets</code>
        </p>
      </header>
      <div className="flex gap-2 items-start flex-wrap">
        <div className="flex-1 min-w-[220px] max-w-md">
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            onEnter={scan}
            placeholder="run_id"
            className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <select
          value={failOn}
          onChange={e => setFailOn(e.target.value)}
          title="Fail threshold — triggers FAIL when findings at or above this severity exist"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
        >
          <option value="">fail-on: none</option>
          {(['critical', 'high', 'medium', 'low', 'info'] as const).map(s => (
            <option key={s} value={s}>fail-on: {s}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={scan}
          disabled={busy || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded font-medium bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-60 transition-colors"
        >
          {busy ? 'Scanning…' : 'Scan'}
        </button>
      </div>
      {err && (
        <div className="text-xs font-mono space-y-1">
          <p className="text-[var(--color-status-failure)]">{err}</p>
          {notFound && (
            <p className="text-[var(--color-text-faint)]">
              No redaction-proof.json for this capsule — run <code>nova redact &lt;capsule&gt;</code> first.
            </p>
          )}
        </div>
      )}
      {data && (
        <div className="space-y-3">
          {/* PASS/FAIL badge — only when a fail-on threshold was set */}
          {data.fail_on && (
            <div className={clsx(
              'rounded border px-3 py-2 text-xs font-mono font-semibold',
              data.triggered
                ? 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] text-[var(--color-status-failure)]'
                : 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] text-[var(--color-status-success)]',
            )}>
              {data.triggered
                ? `✗ FAIL — ${data.triggered_count} finding${data.triggered_count !== 1 ? 's' : ''} at or above ${data.fail_on}`
                : `✓ PASS — no findings at or above ${data.fail_on}`}
            </div>
          )}
          <div className="flex items-center gap-2 flex-wrap">
            <span className={clsx(
              'text-sm font-mono font-semibold',
              total === 0 ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
            )}>
              {total === 0 ? '✓ Clean' : `⚠ ${total} finding${total !== 1 ? 's' : ''}`}
            </span>
            {(['critical', 'high', 'medium', 'low', 'info'] as const).map(s =>
              (bySev[s] ?? 0) > 0
                ? (
                  <span
                    key={s}
                    className={`inline-block font-mono text-[var(--text-2xs)] uppercase px-1.5 py-px rounded border ${SEVERITY_STYLE[s] ?? SEVERITY_STYLE.info}`}
                  >
                    {s} ×{bySev[s]}
                  </span>
                )
                : null
            )}
          </div>
          {data.findings.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono text-[var(--color-text-muted)]">
                <thead>
                  <tr className="text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                    <th className="text-left pb-1.5 pr-3">rule_id</th>
                    <th className="text-left pb-1.5 pr-3">severity</th>
                    <th className="text-left pb-1.5 pr-3">target_ref</th>
                    <th className="text-left pb-1.5">strategy</th>
                  </tr>
                </thead>
                <tbody>
                  {data.findings.map((f, i) => (
                    <tr key={i} className="border-b border-[var(--color-border)] last:border-0">
                      <td className="py-1.5 pr-3 text-[var(--color-text)]">{f.rule_id}</td>
                      <td className="py-1.5 pr-3"><SeverityBadge severity={f.severity} /></td>
                      <td className="py-1.5 pr-3 break-all">{f.target_ref}</td>
                      <td className="py-1.5">{f.redaction_strategy}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
