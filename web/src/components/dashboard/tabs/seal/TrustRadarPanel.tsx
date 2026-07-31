/**
 * ADR-0173 — trust radar for one capsule.
 *
 * Renders the shipped `/api/runs/{id}/trust-radar` projection as a polar glyph
 * plus a per-axis table. The honesty contract is the point of the view: an
 * axis the capsule cannot evidence reads `n/a` (unverified), never `fail`.
 */
import { useState } from 'react';
import { api, type TrustRadar, type RadarVerdict } from '../../../../lib/api';
import { useMutation } from '../../../../lib/useMutation';
import { SuggestInput } from '../../../ui/SuggestInput';
import TrustRadarGlyph from '../../../ui/TrustRadarGlyph';
import { Badge, type BadgeTone } from '../../../ui/primitives';
import PanelScaffold from '../../PanelScaffold';

const VERDICT_TONE: Record<RadarVerdict, BadgeTone> = {
  attested: 'success',
  partial: 'pending',
  critical: 'danger',
  unsealed: 'neutral',
};

const VERDICT_HINT: Record<RadarVerdict, string> = {
  attested: 'sealed — every applicable guarantee met',
  partial: 'sealed, no seal-integrity failure, but some guarantee is incomplete',
  critical: 'a seal-integrity guarantee (signature / log integrity) failed',
  unsealed: 'no signature guarantee — this capsule cannot be attested',
};

const AXIS_STATE_TONE: Record<string, BadgeTone> = {
  ok: 'success',
  warn: 'pending',
  fail: 'danger',
  na: 'neutral',
};

export default function TrustRadarPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const radar = useMutation<[string], TrustRadar>(api.getRunTrustRadar, {
    silentSuccess: true,
  });
  const result = radar.result;

  return (
    <PanelScaffold
      id="trust-radar"
      title="Trust Radar"
      capBadge="ADR-0173"
      subtitle="Which trust guarantees this capsule can actually evidence. An unverifiable guarantee reads n/a — never fail."
      cli={`nova trust-radar ${runId || '<run_id>'}`}
      form={
        <SuggestInput
          value={runId}
          onChange={setRunId}
          suggestions={runIds}
          onEnter={() => runId.trim() && radar.run(runId.trim())}
          placeholder="capsule run_id"
          className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
      }
      onSubmit={() => runId.trim() && radar.run(runId.trim())}
      submitLabel="Plot radar"
      submitDisabled={!runId.trim()}
      pending={radar.pending}
      error={radar.error}
    >
      {result && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Badge tone={VERDICT_TONE[result.verdict]} dot>
              {result.verdict}
            </Badge>
            <span className="text-[var(--text-2xs)] text-[var(--color-text-faint)]">
              {VERDICT_HINT[result.verdict]}
            </span>
          </div>

          <div className="flex flex-wrap items-start gap-4">
            <TrustRadarGlyph axes={result.axes} size={240} />

            <table className="text-[11px] font-mono flex-1 min-w-[16rem]">
              <thead>
                <tr className="text-[var(--text-2xs)] uppercase tracking-wider text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
                  <th className="text-left font-medium py-1">guarantee</th>
                  <th className="text-right font-medium py-1">reach</th>
                  <th className="text-right font-medium py-1">state</th>
                </tr>
              </thead>
              <tbody>
                {result.axes.map((axis) => (
                  <tr key={axis.key} className="border-b border-[var(--color-border)] last:border-0">
                    <td className="py-1 text-[var(--color-text)]">{axis.label}</td>
                    <td className="py-1 text-right tabular-nums text-[var(--color-text-muted)]">
                      {axis.value === null ? '—' : `${Math.round(axis.value * 100)}%`}
                    </td>
                    <td className="py-1 text-right">
                      <Badge tone={AXIS_STATE_TONE[axis.state] ?? 'neutral'}>
                        {axis.state === 'na' ? 'n/a' : axis.state}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </PanelScaffold>
  );
}
