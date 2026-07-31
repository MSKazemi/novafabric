/**
 * ADR-0174 — redaction x-ray for one capsule.
 *
 * A heat overlay of field-protection state across the capsule's field paths,
 * plus the coverage meter. ADR-0174 §1 invariant, enforced by the payload
 * itself: paths and states only — a field VALUE is never fetched or shown.
 * §5: a field with no scan metadata reads `unknown`, never asserted as clear.
 */
import { useMemo, useState } from 'react';
import { api, type FieldState, type XRayReport } from '../../../../lib/api';
import { useMutation } from '../../../../lib/useMutation';
import { SuggestInput } from '../../../ui/SuggestInput';
import { Badge, type BadgeTone } from '../../../ui/primitives';
import PanelScaffold from '../../PanelScaffold';

const STATE_TONE: Record<FieldState, BadgeTone> = {
  clear: 'neutral',
  redacted: 'success',
  secret_scrubbed: 'accent',
  never_captured: 'info',
  unknown: 'pending',
};

const STATE_SWATCH: Record<FieldState, string> = {
  clear: 'bg-[var(--color-faint-tint)]',
  redacted: 'bg-[var(--color-success-tint)]',
  secret_scrubbed: 'bg-[var(--color-accent-tint-strong)]',
  never_captured: 'bg-[color-mix(in_oklab,var(--color-edge-derived-from)_18%,transparent)]',
  unknown: 'bg-[var(--color-pending-tint)]',
};

const STATE_HINT: Record<FieldState, string> = {
  clear: 'captured verbatim (not sensitive)',
  redacted: 'value present but masked by policy',
  secret_scrubbed: 'detected secret, removed',
  never_captured: 'not captured by design (ADR-0021 §4)',
  unknown: 'scan metadata absent — never asserted as clear',
};

const ORDER: FieldState[] = [
  'unknown',
  'secret_scrubbed',
  'redacted',
  'never_captured',
  'clear',
];

function CoverageMeter({ report }: { report: XRayReport }) {
  if (report.coverage === null) {
    return (
      <p className="text-2xs text-[var(--color-text-faint)]">
        No sensitive surface detected — coverage is undefined rather than 100%.
      </p>
    );
  }
  const pct = Math.round(report.coverage * 100);
  const tone =
    pct === 100
      ? 'var(--color-status-success)'
      : pct >= 80
        ? 'var(--color-status-pending)'
        : 'var(--color-status-failure)';
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-2xs font-mono">
        <span className="text-[var(--color-text-faint)] uppercase tracking-wider">
          protection coverage
        </span>
        <span className="tabular-nums text-[var(--color-text)]">
          {report.sensitive_protected} / {report.sensitive_total} sensitive fields · {pct}%
        </span>
      </div>
      <div
        className="h-1.5 rounded-full bg-[var(--color-bg-sunken)] overflow-hidden"
        role="meter"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Protection coverage"
      >
        <div className="h-full rounded-full" style={{ width: `${pct}%`, background: tone }} />
      </div>
    </div>
  );
}

export default function RedactionXrayPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [filter, setFilter] = useState<FieldState | 'all'>('all');
  const xray = useMutation<[string], XRayReport>(api.getRunRedactionXray, {
    silentSuccess: true,
  });
  const report = xray.result;

  const shown = useMemo(
    () => (report ? report.fields.filter((f) => filter === 'all' || f.state === filter) : []),
    [report, filter],
  );

  return (
    <PanelScaffold
      id="redaction-xray"
      title="Redaction X-Ray"
      capBadge="ADR-0174"
      subtitle="Field-protection overlay. Paths and states only — a field value is never returned by this API."
      cli={`nova redaction-xray ${runId || '<run_id>'}`}
      form={
        <SuggestInput
          value={runId}
          onChange={setRunId}
          suggestions={runIds}
          onEnter={() => runId.trim() && xray.run(runId.trim())}
          placeholder="capsule run_id"
          className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
      }
      onSubmit={() => runId.trim() && xray.run(runId.trim())}
      submitLabel="Scan fields"
      submitDisabled={!runId.trim()}
      pending={xray.pending}
      error={xray.error}
    >
      {report && (
        <div className="space-y-3">
          <CoverageMeter report={report} />

          {/* Legend doubles as the state filter */}
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={() => setFilter('all')}
              className={`text-2xs px-1.5 py-px rounded border transition-colors ${
                filter === 'all'
                  ? 'border-[var(--color-accent)] text-[var(--color-accent)]'
                  : 'border-[var(--color-border)] text-[var(--color-text-faint)] hover:text-[var(--color-text)]'
              }`}
            >
              all {report.fields.length}
            </button>
            {ORDER.filter((s) => (report.counts[s] ?? 0) > 0).map((state) => (
              <button
                key={state}
                type="button"
                onClick={() => setFilter(filter === state ? 'all' : state)}
                title={STATE_HINT[state]}
                className={`flex items-center gap-1 text-2xs px-1.5 py-px rounded border transition-colors ${
                  filter === state
                    ? 'border-[var(--color-accent)]'
                    : 'border-[var(--color-border)] hover:border-[var(--color-border-strong)]'
                }`}
              >
                <span
                  aria-hidden="true"
                  className={`inline-block w-2 h-2 rounded-sm ${STATE_SWATCH[state]}`}
                />
                <span className="text-[var(--color-text-muted)]">{state.replace('_', ' ')}</span>
                <span className="font-mono tabular-nums text-[var(--color-text-faint)]">
                  {report.counts[state]}
                </span>
              </button>
            ))}
          </div>

          {report.fields.length === 0 ? (
            <p className="text-[11px] text-[var(--color-text-faint)] italic py-2">
              No scan metadata for this capsule — it was captured without the masking
              pipeline. &ldquo;Nothing was scanned&rdquo; is a real answer, not an error.
            </p>
          ) : (
            <div className="max-h-72 overflow-y-auto rounded border border-[var(--color-border)]">
              {shown.map((field) => (
                <div
                  key={field.path}
                  className={`flex items-center justify-between gap-2 px-2.5 py-1 border-b border-[var(--color-border)] last:border-0 ${STATE_SWATCH[field.state]}`}
                  title={STATE_HINT[field.state]}
                >
                  <code className="text-[11px] font-mono text-[var(--color-text)] truncate">
                    {field.path}
                  </code>
                  <Badge tone={STATE_TONE[field.state]}>{field.state.replace('_', ' ')}</Badge>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </PanelScaffold>
  );
}
