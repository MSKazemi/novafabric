/**
 * Audit Verify panel (`nova audit verify`) — exemplar of the
 * PanelScaffold + useMutation pattern. Same API call and rendered result data
 * as the pre-split bespoke panel; only the chrome comes from the scaffold.
 * A client-side JSON.parse failure surfaces through the same error box.
 */
import { useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { useMutation } from '../../../../lib/useMutation';
import PanelScaffold from '../../PanelScaffold';

export default function AuditVerifyPanel() {
  const [reportJson, setReportJson] = useState('');

  const verify = useMutation(
    async () => {
      const parsed: unknown = JSON.parse(reportJson.trim());
      return api.auditVerify(parsed as Record<string, unknown>);
    },
    { silentSuccess: true, silentError: true },
  );
  const result = verify.result;

  return (
    <PanelScaffold
      id="audit-verify"
      title="Audit Report Verify"
      subtitle={
        <>
          Validate an AuditReport JSON against the schema — <code className="font-mono">nova audit verify</code>
        </>
      }
      cli="nova audit verify audit-report.json"
      form={
        <textarea
          value={reportJson}
          onChange={e => setReportJson(e.target.value)}
          placeholder='{"report_id": "...", "profile": "nist-ai-rmf", ...}'
          rows={5}
          className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
      }
      onSubmit={() => void verify.run()}
      submitLabel="Verify"
      submitDisabled={!reportJson.trim()}
      pending={verify.pending}
      error={verify.error}
    >
      {result && (
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <span className={clsx(
              'text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded',
              result.valid
                ? 'text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)]'
                : 'text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)]',
            )}>
              {result.valid ? 'valid' : 'invalid'}
            </span>
            {result.report_id && <span className="text-[10px] font-mono text-[var(--color-text-faint)]">{result.report_id}</span>}
            {result.profile && <span className="text-[10px] font-mono text-[var(--color-text-faint)]">{result.profile}</span>}
            {result.overall_score !== undefined && (
              <span className="text-[10px] font-mono text-[var(--color-text-faint)]">{(result.overall_score * 100).toFixed(1)}%</span>
            )}
          </div>
          {result.errors.length > 0 && (
            <ul className="space-y-0.5">
              {result.errors.map((e, i) => (
                <li key={i} className="text-[10px] font-mono text-[var(--color-status-failure)]">• {e}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </PanelScaffold>
  );
}
