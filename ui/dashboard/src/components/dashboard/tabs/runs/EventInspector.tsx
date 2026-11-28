/**
 * Event inspector (issue #268, first slice): the run's model calls, HTTP
 * attempts and tool calls as one time-ordered list, and the selected event's
 * fields — each marked *recorded* (read verbatim from the capsule) or
 * *derived* (computed by this view) — with the record's provenance, masked
 * values and related calls.
 *
 * Data: only the capsule `GET /api/runs/{id}` already returned. The selected
 * event lives in the URL (`?event=`), so a link opens on the same event; a
 * link to an event this capsule does not hold says so instead of picking
 * another one silently.
 *
 * Keyboard: the list is a listbox. ↑/↓ (and j/k, unless single-key shortcuts
 * are off) move the selection, Home/End jump to the ends. Those keys are kept
 * inside the list, so they do not also move the run selection.
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react';
import { clsx } from 'clsx';
import type { FullCapsule } from '../../../../lib/api';
import { singleKeyShortcutsEnabled } from '../../../../lib/shortcuts';
import StatusPill from '../../../ui/primitives/StatusPill';
import Button from '../../../ui/primitives/Button';
import EmptyState from '../../../ui/EmptyState';
import {
  PROVENANCE_HELP, PROVENANCE_LABEL, STREAM_LABEL, firstObservedFailure, runEventsFor,
  type RunEvent,
} from './eventModel';

/** Rows rendered at once; the rest load on request (or when the selection moves past them). */
export const EVENT_PAGE = 200;
/** The raw record is shown up to this many characters. */
const RAW_MAX_CHARS = 64 * 1024;

const STREAM_FILE: Record<RunEvent['stream'], string> = {
  model: 'model-calls.jsonl',
  transport: 'model-calls.jsonl (transport record)',
  tool: 'tool-calls.jsonl',
};

function optionId(key: string): string {
  return `run-event-${key.replace(/[^A-Za-z0-9_-]/g, '_')}`;
}

function fmtOffset(ms: number | null, t0: number | null): string {
  if (ms === null || t0 === null) return 'time unknown';
  const d = ms - t0;
  return d < 1000 ? `+${d} ms` : `+${(d / 1000).toFixed(2)} s`;
}

function SourceTag({ source }: { source: 'recorded' | 'derived' }) {
  return (
    <span
      className={clsx(
        'ml-1.5 text-[9px] uppercase tracking-wider px-1 py-px rounded border',
        source === 'recorded'
          ? 'border-[var(--color-border)] text-[var(--color-text-faint)]'
          : 'border-[color-mix(in_oklab,var(--color-accent)_40%,transparent)] text-[var(--color-accent)]',
      )}
    >
      {source}
    </span>
  );
}

function Field({ label, source, children }: { label: string; source: 'recorded' | 'derived'; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[8.5rem_1fr] gap-2 py-1 text-xs min-w-0">
      <dt className="text-[var(--color-text-faint)]">
        {label}
        <SourceTag source={source} />
      </dt>
      <dd className="min-w-0 break-words text-[var(--color-text)]">{children}</dd>
    </div>
  );
}

function NotRecorded({ children = 'not recorded' }: { children?: ReactNode }) {
  return <span className="italic text-[var(--color-text-faint)]">{children}</span>;
}

function ProvenanceBadge({ event }: { event: RunEvent }) {
  return (
    <span
      title={PROVENANCE_HELP[event.provenance]}
      className={clsx(
        'shrink-0 text-[9px] uppercase tracking-wider px-1 py-px rounded border',
        event.provenance === 'recorded'
          ? 'border-[var(--color-border)] text-[var(--color-text-faint)]'
          : 'border-[color-mix(in_oklab,var(--color-status-pending)_45%,transparent)] text-[var(--color-status-pending)]',
      )}
    >
      {PROVENANCE_LABEL[event.provenance]}
      {event.replayPlaceholder ? ` · ${event.replayPlaceholder}` : ''}
    </span>
  );
}

function outcomeText(ev: RunEvent): string {
  if (ev.outcome === 'ok') return 'ok';
  if (ev.outcome === 'failed') return 'failed';
  return ev.replayPlaceholder === 'live' ? 'outcome not observed' : 'outcome unknown';
}

function RawRecord({ record }: { record: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const text = useMemo(() => (open ? JSON.stringify(record, null, 2) : ''), [open, record]);
  return (
    <div className="pt-2">
      <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen(o => !o)}>
        {open ? 'Hide record as stored' : 'Show record as stored'}
      </Button>
      {open && (
        <>
          <pre className="mt-1 max-h-80 overflow-auto rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-2 text-[10px] font-mono whitespace-pre-wrap break-all">
            {text.length > RAW_MAX_CHARS ? text.slice(0, RAW_MAX_CHARS) : text}
          </pre>
          {text.length > RAW_MAX_CHARS && (
            <p className="text-[10px] text-[var(--color-text-faint)]">
              Showing the first {RAW_MAX_CHARS.toLocaleString()} of {text.length.toLocaleString()} characters.
            </p>
          )}
        </>
      )}
    </div>
  );
}

function RelatedButton({ id, byRecordId, onSelect }: {
  id: string;
  byRecordId: Map<string, RunEvent>;
  onSelect: (key: string) => void;
}) {
  const target = byRecordId.get(id);
  if (!target) {
    return <span className="font-mono">{id} <NotRecorded>(not in this capsule)</NotRecorded></span>;
  }
  return (
    <button
      type="button"
      onClick={() => onSelect(target.key)}
      className="font-mono text-[var(--color-accent)] hover:underline text-left"
    >
      {STREAM_LABEL[target.stream]} {target.label} ({id})
    </button>
  );
}

function EventDetail({ event, byRecordId, onSelect }: {
  event: RunEvent;
  byRecordId: Map<string, RunEvent>;
  onSelect: (key: string) => void;
}) {
  const triggeredTools = [...byRecordId.values()].filter(
    ev => ev.stream === 'tool' && event.recordId !== null && ev.agentCallId === event.recordId,
  );
  const choiceIds = [...new Set([...event.toolChoiceIds, ...triggeredTools.map(t => t.recordId as string)])];
  return (
    <section aria-label="Event detail" className="min-w-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3">
      <h4 className="text-xs font-medium text-[var(--color-text)] flex items-center gap-2 flex-wrap">
        <StatusPill status={event.outcome === 'unknown' ? null : event.outcome === 'ok' ? 'success' : 'failure'} variant="dot" />
        <span>{STREAM_LABEL[event.stream]}</span>
        <code className="font-mono">{event.label}</code>
        <ProvenanceBadge event={event} />
      </h4>
      <dl className="mt-2 divide-y divide-[var(--color-border)]">
        <Field label="Provenance" source="derived">
          {PROVENANCE_LABEL[event.provenance]} — {PROVENANCE_HELP[event.provenance]}
        </Field>
        <Field label="Source file" source="recorded">
          <code className="font-mono">{STREAM_FILE[event.stream]}</code>, record #{event.index + 1}
        </Field>
        <Field label="Record id" source="recorded">
          {event.recordId ? <code className="font-mono">{event.recordId}</code> : <NotRecorded />}
        </Field>
        <Field label="Status" source="recorded">
          {event.status ? <code className="font-mono">{event.status}</code> : <NotRecorded />}
        </Field>
        <Field label="Outcome" source="derived">{outcomeText(event)}</Field>
        <Field label="Started at" source="recorded">
          {event.startedAt ? <code className="font-mono">{event.startedAt}</code> : <NotRecorded />}
          {event.startedAt && event.startedMs === null && <NotRecorded> (not a readable time)</NotRecorded>}
        </Field>
        <Field label="Duration" source="recorded">
          {event.durationMs !== null ? `${event.durationMs} ms` : <NotRecorded />}
        </Field>
        <Field label="Severity" source="recorded">
          {event.logLevel
            ? <>{event.logLevel}{event.logLevelSource ? <> (set by {event.logLevelSource})</> : null}</>
            : <NotRecorded>not recorded (filters read an absent level as info)</NotRecorded>}
        </Field>
        {event.statusMessage && <Field label="Status message" source="recorded">{event.statusMessage}</Field>}
        <Field label="Error" source="recorded">
          {event.error
            ? <><code className="font-mono">{event.error.type ?? 'error'}</code>{event.error.message ? `: ${event.error.message}` : ''}</>
            : <NotRecorded>{event.outcome === 'failed' ? 'no error detail recorded' : 'none recorded'}</NotRecorded>}
        </Field>
        <Field label="Masked values" source="derived">
          {event.redactions.length > 0
            ? <>Masked at capture by rule{event.redactions.length === 1 ? '' : 's'}{' '}
                {event.redactions.map((r, i) => <span key={r}>{i > 0 ? ', ' : ''}<code className="font-mono">{r}</code></span>)}</>
            : <NotRecorded>no [REDACTED:…] marker in this record</NotRecorded>}
        </Field>
        {event.parentSpanId && (
          <Field label="Parent span" source="recorded"><code className="font-mono">{event.parentSpanId}</code></Field>
        )}
        {event.agentCallId && (
          <Field label="Chosen by" source="derived">
            <RelatedButton id={event.agentCallId} byRecordId={byRecordId} onSelect={onSelect} />
          </Field>
        )}
        {event.logicalCallId && event.stream === 'transport' && (
          <Field label="Attempt of" source="derived">
            <RelatedButton id={event.logicalCallId} byRecordId={byRecordId} onSelect={onSelect} />
          </Field>
        )}
        {event.stream === 'model' && choiceIds.length > 0 && (
          <Field label="Tool calls" source="derived">
            <ul className="space-y-0.5">
              {choiceIds.map(id => (
                <li key={id}><RelatedButton id={id} byRecordId={byRecordId} onSelect={onSelect} /></li>
              ))}
            </ul>
          </Field>
        )}
      </dl>
      <p className="mt-2 text-[10px] text-[var(--color-text-faint)]">
        Related calls are joined on the ids the records carry (<code className="font-mono">agent_call_id</code>,{' '}
        <code className="font-mono">tool_choice_call_ids</code>, the logical call id); a join is not a claim that one call caused another.
      </p>
      <RawRecord record={event.record} />
    </section>
  );
}

export interface EventInspectorProps {
  capsule: FullCapsule;
  /** The `?event=` value; '' selects the first observed failure, else the first event. */
  selectedKey?: string;
  onSelectKey?: (key: string) => void;
  /** Open the Integrity view (seal verification). */
  onOpenIntegrity?: () => void;
}

export default function EventInspector({ capsule, selectedKey, onSelectKey, onOpenIntegrity }: EventInspectorProps) {
  const [localKey, setLocalKey] = useState('');
  const key = selectedKey ?? localKey;
  const select = (k: string) => { if (onSelectKey) onSelectKey(k); else setLocalKey(k); };
  const [failuresOnly, setFailuresOnly] = useState(false);
  const [limit, setLimit] = useState(EVENT_PAGE);
  const [announcement, setAnnouncement] = useState('');
  const listRef = useRef<HTMLUListElement>(null);

  const model = runEventsFor(capsule);
  const { events } = model;
  const firstFailure = useMemo(() => firstObservedFailure(events), [events]);
  const byKey = useMemo(() => new Map(events.map(ev => [ev.key, ev])), [events]);
  const byRecordId = useMemo(() => {
    const m = new Map<string, RunEvent>();
    for (const ev of events) if (ev.recordId && !m.has(ev.recordId) && ev.stream !== 'transport') m.set(ev.recordId, ev);
    return m;
  }, [events]);
  const failedCount = useMemo(() => events.filter(ev => ev.outcome === 'failed').length, [events]);
  const visible = useMemo(
    () => (failuresOnly ? events.filter(ev => ev.outcome === 'failed') : events),
    [events, failuresOnly],
  );

  const missing = key !== '' && !byKey.has(key);
  const selected: RunEvent | null = key === '' ? (firstFailure ?? events[0] ?? null) : (byKey.get(key) ?? null);
  const selIdx = selected ? visible.indexOf(selected) : -1;
  const renderCount = Math.min(visible.length, Math.max(limit, selIdx + 1));
  const t0 = events.find(ev => ev.startedMs !== null)?.startedMs ?? null;

  // A new run starts with the first page again.
  useEffect(() => { setLimit(EVENT_PAGE); setFailuresOnly(false); }, [capsule.run_id]);

  // Keep the keyboard selection in view (only after a key press: opening the
  // view must not scroll the page).
  const scrollPending = useRef(false);
  useEffect(() => {
    if (!selected || !scrollPending.current) return;
    scrollPending.current = false;
    const el = document.getElementById(optionId(selected.key));
    if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' });
  }, [selected]);

  function move(to: number) {
    if (visible.length === 0) return;
    const next = visible[Math.max(0, Math.min(visible.length - 1, to))];
    scrollPending.current = true;
    select(next.key);
  }

  function onListKey(e: ReactKeyboardEvent<HTMLUListElement>) {
    const modifier = e.metaKey || e.ctrlKey || e.altKey;
    const letter = !modifier && singleKeyShortcutsEnabled() && (e.key === 'j' || e.key === 'k');
    let handled = true;
    if (e.key === 'ArrowDown' || (letter && e.key === 'j')) move(selIdx + 1);
    else if (e.key === 'ArrowUp' || (letter && e.key === 'k')) move(selIdx < 0 ? 0 : selIdx - 1);
    else if (e.key === 'Home') move(0);
    else if (e.key === 'End') move(visible.length - 1);
    else handled = false;
    if (handled) {
      e.preventDefault();
      // Keep j/k inside the list: the Runs view also binds them, to move the run selection.
      e.stopPropagation();
    }
  }

  function jumpToFirstFailure() {
    if (!firstFailure) return;
    scrollPending.current = true;
    select(firstFailure.key);
    setAnnouncement(`Selected the first observed failure: ${STREAM_LABEL[firstFailure.stream]} ${firstFailure.label}.`);
    listRef.current?.focus();
  }

  const counts = {
    model: events.filter(ev => ev.stream === 'model').length,
    transport: events.filter(ev => ev.stream === 'transport').length,
    tool: events.filter(ev => ev.stream === 'tool').length,
  };

  return (
    <section aria-label="Run events" className="space-y-3">
      <header className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3 space-y-2">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div>
            <h3 className="text-xs font-medium text-[var(--color-text)]">Events</h3>
            <p className="text-[10px] text-[var(--color-text-faint)] font-mono mt-0.5" data-testid="event-counts">
              {events.length} event{events.length === 1 ? '' : 's'}: {counts.model} model call{counts.model === 1 ? '' : 's'}
              {counts.transport > 0 && `, ${counts.transport} HTTP attempt${counts.transport === 1 ? '' : 's'}`}
              , {counts.tool} tool call{counts.tool === 1 ? '' : 's'} · {failedCount} failed
            </p>
          </div>
          <div className="flex items-center gap-1.5 flex-wrap">
            <Button size="sm" variant="secondary" onClick={jumpToFirstFailure} disabled={!firstFailure}>
              {firstFailure ? 'Jump to first failure' : 'No failed event'}
            </Button>
            <label className="inline-flex items-center gap-1 text-[10px] text-[var(--color-text-muted)]">
              <input type="checkbox" checked={failuresOnly} onChange={e => setFailuresOnly(e.target.checked)} />
              Failed events only
            </label>
          </div>
        </div>
        <p className="text-[10px] text-[var(--color-text-muted)]">
          Read from the capsule as stored. This view does not check the seal
          {onOpenIntegrity
            ? <> — <button type="button" className="text-[var(--color-accent)] hover:underline" onClick={onOpenIntegrity}>verify it in Integrity</button>.</>
            : '.'}
          {' '}Order: recorded <code className="font-mono">started_at</code>.
        </p>
        {model.replay && (
          <p className="text-[10px] text-[var(--color-status-pending)]" data-testid="event-replay-note">
            This capsule is a {model.replay.mode ?? 'replay'} replay of run <code className="font-mono">{model.replay.of}</code>:
            its records were written while the replay ran, not by the original run.
          </p>
        )}
        {model.gaps.length > 0 && (
          <ul aria-label="Evidence gaps" className="list-disc pl-4 text-[10px] text-[var(--color-text-muted)] space-y-0.5">
            {model.gaps.map(g => <li key={g}>{g}</li>)}
          </ul>
        )}
        <details className="text-[10px] text-[var(--color-text-muted)]">
          <summary className="cursor-pointer text-[var(--color-text-faint)]">What the labels mean</summary>
          <ul className="mt-1 space-y-0.5 pl-1">
            {(Object.keys(PROVENANCE_LABEL) as Array<keyof typeof PROVENANCE_LABEL>).map(p => (
              <li key={p}><strong>{PROVENANCE_LABEL[p]}</strong> — {PROVENANCE_HELP[p]}</li>
            ))}
            <li><strong>recorded / derived</strong> — a recorded field is read verbatim from the record; a derived one (outcome,
              provenance, masked values, related calls, the time offset) is computed by this view from recorded fields.</li>
            <li><strong>First observed failure</strong> — the earliest event by recorded start time whose recorded status is not
              <code className="font-mono"> success</code>. It is where the evidence first shows a failure, not a root cause.</li>
            <li>Keys: ↑/↓ or j/k move between events, Home/End jump to the ends.</li>
          </ul>
        </details>
      </header>

      <p className="sr-only" aria-live="polite">{announcement}</p>

      {missing && (
        <p role="status" className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-2 text-xs text-[var(--color-text-muted)]" data-testid="event-missing">
          Event <code className="font-mono">{key}</code> is not in this run&apos;s capsule. The link may point to another run,
          or to a record this capsule does not hold. Pick an event below.
        </p>
      )}

      {events.length === 0 ? (
        <EmptyState
          variant="inline"
          message="No model calls, HTTP attempts or tool calls were recorded for this run."
          hint="The Inspect view shows the manifest and the trace."
        />
      ) : (
        <div className="grid lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-3 items-start">
          <div className="min-w-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)]">
            {visible.length === 0 ? (
              <p className="p-3 text-xs text-[var(--color-text-muted)]">No event recorded a non-success status.</p>
            ) : (
              <ul
                ref={listRef}
                role="listbox"
                aria-label="Run events in recorded order"
                tabIndex={0}
                aria-activedescendant={selected && selIdx >= 0 ? optionId(selected.key) : undefined}
                onKeyDown={onListKey}
                className="max-h-[60vh] overflow-y-auto divide-y divide-[var(--color-border)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
              >
                {visible.slice(0, renderCount).map((ev, i) => (
                  <li
                    key={ev.key}
                    id={optionId(ev.key)}
                    role="option"
                    aria-selected={selected?.key === ev.key}
                    aria-posinset={i + 1}
                    aria-setsize={visible.length}
                    onClick={() => select(ev.key)}
                    className={clsx(
                      'flex items-center gap-2 px-2 py-1.5 text-xs cursor-pointer min-w-0',
                      selected?.key === ev.key
                        ? 'bg-[color-mix(in_oklab,var(--color-accent)_12%,transparent)]'
                        : 'hover:bg-[var(--color-surface-hover)]',
                    )}
                  >
                    <StatusPill status={ev.outcome === 'unknown' ? null : ev.outcome === 'ok' ? 'success' : 'failure'} variant="dot" />
                    <span className="shrink-0 w-16 font-mono text-[10px] text-[var(--color-text-faint)] tabular-nums">
                      {fmtOffset(ev.startedMs, t0)}
                    </span>
                    <span className="shrink-0 text-[10px] text-[var(--color-text-muted)]">{STREAM_LABEL[ev.stream]}</span>
                    <span className="truncate font-mono">{ev.label}</span>
                    <span className="sr-only">, {outcomeText(ev)}</span>
                    {firstFailure?.key === ev.key && (
                      <span className="shrink-0 text-[9px] uppercase tracking-wider text-[var(--color-status-failure)]">first failure</span>
                    )}
                    {ev.redactions.length > 0 && (
                      <span className="shrink-0 text-[9px] text-[var(--color-text-faint)]" title={`Masked: ${ev.redactions.join(', ')}`}>
                        masked
                      </span>
                    )}
                    <span className="ml-auto"><ProvenanceBadge event={ev} /></span>
                  </li>
                ))}
              </ul>
            )}
            {renderCount < visible.length && (
              <div className="flex items-center justify-between gap-2 px-2 py-1.5 border-t border-[var(--color-border)] text-[10px] text-[var(--color-text-muted)]">
                <span data-testid="event-window">Showing {renderCount.toLocaleString()} of {visible.length.toLocaleString()} events</span>
                <Button size="sm" variant="ghost" onClick={() => setLimit(renderCount + EVENT_PAGE)}>
                  Show {Math.min(EVENT_PAGE, visible.length - renderCount)} more
                </Button>
              </div>
            )}
          </div>
          {selected
            ? <EventDetail event={selected} byRecordId={byRecordId} onSelect={k => select(k)} />
            : <p className="text-xs text-[var(--color-text-muted)]">Select an event to see where it came from.</p>}
        </div>
      )}
    </section>
  );
}
