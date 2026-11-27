import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { clsx } from 'clsx';

import {
  buildAlignedComparison,
  describeDivergence,
  describeRow,
  describeTimeline,
  diffCliCommand,
  verdictLabel,
  type AlignedComparison,
  type AlignedRow,
  type StreamKey,
  type Verdict,
} from './alignedComparison';

/**
 * Aligned A/B comparison (issue #270, first slice): the server's model-call and
 * tool-call pairs in alignment order, the first divergent step highlighted and
 * scrolled to, every verdict in words, and the equivalent `nova diff` command.
 *
 * Read-only and content-free: it shows record ids, the engine's verdict flags
 * and tool names — never a prompt, response, argument or result.
 */

const VERDICT_MARK: Record<Verdict, string> = { changed: '~', added: '+', removed: '−', unchanged: '=' };

const VERDICT_TONE: Record<Verdict, string> = {
  changed: 'border-[color-mix(in_oklab,var(--color-status-pending)_40%,transparent)] text-[var(--color-status-pending)]',
  added: 'border-[color-mix(in_oklab,var(--color-status-success)_40%,transparent)] text-[var(--color-status-success)]',
  removed: 'border-[color-mix(in_oklab,var(--color-status-failure)_40%,transparent)] text-[var(--color-status-failure)]',
  unchanged: 'border-[var(--color-border)] text-[var(--color-text-faint)]',
};

const STREAM_TITLE: Record<StreamKey, string> = { model_calls: 'Model calls', tool_calls: 'Tool calls' };

function VerdictBadge({ verdict }: { verdict: Verdict }) {
  return (
    <span className={clsx('inline-flex items-center gap-1 rounded border px-1.5 py-px text-[10px] font-medium whitespace-nowrap', VERDICT_TONE[verdict])}>
      <span aria-hidden="true">{VERDICT_MARK[verdict]}</span>
      {verdictLabel(verdict)}
    </span>
  );
}

function RecordCell({ id, placeholder, status }: { id: string | null; placeholder: string | null; status?: string | null }) {
  if (id === null && placeholder === null) return <span className="text-[var(--color-text-faint)]">—</span>;
  return (
    <span className="font-mono break-all">
      {id ?? <span className="text-[var(--color-text-faint)]">no record id</span>}
      {status ? <span className="ml-1 text-[var(--color-text-faint)]">({status})</span> : null}
      {placeholder ? <span className="ml-1 text-[var(--color-text-faint)]">replay placeholder: {placeholder}</span> : null}
    </span>
  );
}

function StreamTable({
  stream, rows, onlyDifferences, rowRef, domId,
}: {
  stream: StreamKey;
  rows: AlignedRow[];
  onlyDifferences: boolean;
  rowRef: (key: string, el: HTMLTableRowElement | null) => void;
  domId: (key: string) => string;
}) {
  const shown = onlyDifferences ? rows.filter(r => r.verdict !== 'unchanged') : rows;
  const counts = { changed: 0, added: 0, removed: 0, unchanged: 0 } as Record<Verdict, number>;
  for (const r of rows) counts[r.verdict] += 1;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-collapse">
        <caption className="text-left text-sm font-medium text-[var(--color-text)] mb-1">
          {STREAM_TITLE[stream]}
          <span className="ml-2 text-[11px] font-normal text-[var(--color-text-muted)]">
            {rows.length} aligned pair{rows.length !== 1 ? 's' : ''}: {counts.changed} changed, {counts.added} added, {counts.removed} removed, {counts.unchanged} unchanged
            {onlyDifferences && counts.unchanged > 0 ? ` (unchanged pairs hidden)` : ''}
          </span>
        </caption>
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-[var(--color-text-faint)]">
            <th scope="col" className="py-1 pr-2 font-medium">Pair</th>
            <th scope="col" className="py-1 pr-2 font-medium">Change</th>
            {stream === 'tool_calls' && <th scope="col" className="py-1 pr-2 font-medium">Tool</th>}
            <th scope="col" className="py-1 pr-2 font-medium">What differs</th>
            <th scope="col" className="py-1 pr-2 font-medium">Run A record</th>
            <th scope="col" className="py-1 font-medium">Run B record</th>
          </tr>
        </thead>
        <tbody>
          {shown.length === 0 && (
            <tr>
              <td colSpan={stream === 'tool_calls' ? 6 : 5} className="py-2 text-[var(--color-text-muted)]">
                {rows.length === 0 ? `Neither run recorded ${stream === 'model_calls' ? 'a model call' : 'a tool call'}.` : 'No differences in this stream.'}
              </td>
            </tr>
          )}
          {shown.map(row => (
            <tr
              key={row.key}
              id={domId(row.key)}
              ref={el => rowRef(row.key, el)}
              tabIndex={row.isFirstDivergence ? -1 : undefined}
              aria-current={row.isFirstDivergence ? 'step' : undefined}
              data-verdict={row.verdict}
              className={clsx(
                'border-t border-[var(--color-border)] align-top focus:outline-2 focus:outline-offset-[-2px] focus:outline-[var(--color-accent)]',
                row.isFirstDivergence && 'bg-[color-mix(in_oklab,var(--color-accent)_12%,transparent)]',
              )}
            >
              <td className="py-1.5 pr-2 font-mono text-[var(--color-text-muted)]">#{row.position}</td>
              <td className="py-1.5 pr-2">
                <div className="flex flex-wrap items-center gap-1">
                  <VerdictBadge verdict={row.verdict} />
                  {row.isFirstDivergence && (
                    <span className="rounded bg-[var(--color-accent)] px-1.5 py-px text-[10px] font-semibold text-[var(--color-accent-fg)] whitespace-nowrap">
                      First divergence
                    </span>
                  )}
                </div>
              </td>
              {stream === 'tool_calls' && <td className="py-1.5 pr-2 font-mono">{row.toolName ?? '—'}</td>}
              <td className="py-1.5 pr-2 text-[var(--color-text)]">{describeRow(row)}</td>
              <td className="py-1.5 pr-2"><RecordCell id={row.idA} placeholder={row.placeholderA} status={stream === 'tool_calls' ? row.statusA : null} /></td>
              <td className="py-1.5"><RecordCell id={row.idB} placeholder={row.placeholderB} status={stream === 'tool_calls' ? row.statusB : null} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CopyCliCommand({ runA, runB }: { runA: string; runB: string }) {
  const [message, setMessage] = useState('');
  const command = diffCliCommand(runA, runB);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setMessage('Command copied.');
    } catch {
      setMessage('Clipboard unavailable. Select the command text and copy it manually.');
    }
  };
  return (
    <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
      <p className="text-[11px] text-[var(--color-text-muted)]">
        Same comparison in a terminal (prints the same first divergence; add <code>--output-format json</code> for the report this view reads):
      </p>
      <div className="mt-1 flex flex-wrap items-center justify-between gap-2">
        <code className="select-all break-all font-mono text-xs text-[var(--color-text)]">{command}</code>
        <button
          type="button"
          aria-label={`Copy CLI command: ${command}`}
          onClick={() => void copy()}
          className="rounded border border-[var(--color-border-strong)] px-2.5 py-1 text-xs font-medium text-[var(--color-text)] hover:bg-[var(--color-bg-raised)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
        >
          Copy command
        </button>
      </div>
      <p role="status" aria-live="polite" className="mt-1 text-[11px] text-[var(--color-text-muted)] min-h-[1em]">{message}</p>
    </div>
  );
}

function OverallVerdict({ c }: { c: AlignedComparison }) {
  if (c.skippedLines > 0) {
    return (
      <p className="text-sm text-[var(--color-status-pending)]">
        Comparison incomplete: {c.skippedLines} record line{c.skippedLines !== 1 ? 's' : ''} could not be read and took no part in it.
        {c.hasChanges ? ' The runs differ in the records that were read.' : ' No difference among the records that were read — that does not make the runs identical.'}
      </p>
    );
  }
  return c.hasChanges
    ? <p className="text-sm text-[var(--color-text)]">The runs differ.</p>
    : <p className="text-sm text-[var(--color-text)]">No differences: the server found no changed, added or removed entry in any section.</p>;
}

export default function AlignedDiffView({ raw, runA, runB }: { raw: unknown; runA?: string; runB?: string }) {
  const comparison = useMemo(() => buildAlignedComparison(raw, { runA, runB }), [raw, runA, runB]);
  const [onlyDifferences, setOnlyDifferences] = useState(false);
  const rowEls = useRef(new Map<string, HTMLTableRowElement>());
  const prefix = useId();
  const headingId = `${prefix}-heading`;
  const domId = useCallback((key: string) => `${prefix}-row-${key}`, [prefix]);
  const rowRef = useCallback((key: string, el: HTMLTableRowElement | null) => {
    if (el) rowEls.current.set(key, el); else rowEls.current.delete(key);
  }, []);

  const fd = comparison.firstDivergence;
  const targetKey = fd.kind === 'found' ? fd.rowKey : null;

  const reveal = useCallback((focus: boolean) => {
    if (!targetKey) return;
    const el = rowEls.current.get(targetKey);
    if (!el) return;
    if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    if (focus) el.focus({ preventScroll: true });
  }, [targetKey]);

  // Scroll the first divergent pair into view when a comparison loads. Focus stays
  // where the user left it; "Go to first divergence" moves it on request.
  useEffect(() => { reveal(false); }, [reveal, comparison]);

  return (
    <section aria-labelledby={headingId} className="space-y-3" data-testid="aligned-diff">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id={headingId} className="text-lg font-medium tracking-tight text-[var(--color-text)]">Aligned comparison</h2>
          <p className="text-[11px] text-[var(--color-text-muted)]">
            Model and tool calls paired by the server's diff engine (the one <code>nova diff</code> runs), in alignment order. Record ids only — no prompt, response or tool content.
          </p>
        </div>
        <label className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
          <input type="checkbox" checked={onlyDifferences} onChange={e => setOnlyDifferences(e.target.checked)} />
          Show only differences
        </label>
      </header>

      <div role="status"><OverallVerdict c={comparison} /></div>

      <div className="rounded border border-[var(--color-border)] p-3" role="group" aria-label="First divergence">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-text-muted)]">First divergence</p>
        {fd.kind === 'found' && (
          <div className="mt-1 space-y-1">
            <p className="text-sm text-[var(--color-text)]">{describeDivergence(fd.detail)}</p>
            <p className="text-xs font-mono text-[var(--color-text-muted)]">
              Run A: {fd.detail.indexA !== null ? `step ${fd.detail.indexA}${fd.detail.recordIdA ? ` · ${fd.detail.recordIdA}` : ''}` : 'no call (added in B)'}
              {' · '}
              Run B: {fd.detail.indexB !== null ? `step ${fd.detail.indexB}${fd.detail.recordIdB ? ` · ${fd.detail.recordIdB}` : ''}` : 'no call (removed in B)'}
            </p>
            <p className="text-[11px] text-[var(--color-text-faint)]">{describeTimeline(fd.detail)}</p>
            {fd.rowKey ? (
              <button
                type="button"
                onClick={() => reveal(true)}
                aria-controls={domId(fd.rowKey)}
                className="mt-1 rounded border border-[var(--color-accent)] px-2.5 py-1 text-xs font-medium text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
              >
                Go to first divergence
              </button>
            ) : (
              <p className="text-[11px] text-[var(--color-text-muted)]">
                The pair is not marked in the table: its records carry no unique id to match it by.
              </p>
            )}
          </div>
        )}
        {fd.kind === 'none' && (
          <p className="mt-1 text-sm text-[var(--color-text)]">
            None in model or tool calls.
            {comparison.hasChanges && (comparison.environmentChanges > 0 || comparison.outputChanges > 0)
              ? ` The runs differ in ${[
                  comparison.environmentChanges > 0 ? `the environment (${comparison.environmentChanges})` : '',
                  comparison.outputChanges > 0 ? `outputs (${comparison.outputChanges})` : '',
                ].filter(Boolean).join(' and ')}: see the structural diff below.`
              : ''}
          </p>
        )}
        {fd.kind === 'not_derivable' && (
          <p className="mt-1 text-sm text-[var(--color-text)]">Not derivable. {fd.why}</p>
        )}
      </div>

      <StreamTable stream="model_calls" rows={comparison.rows.model_calls} onlyDifferences={onlyDifferences} rowRef={rowRef} domId={domId} />
      <StreamTable stream="tool_calls" rows={comparison.rows.tool_calls} onlyDifferences={onlyDifferences} rowRef={rowRef} domId={domId} />

      <CopyCliCommand runA={comparison.runA} runB={comparison.runB} />
    </section>
  );
}
