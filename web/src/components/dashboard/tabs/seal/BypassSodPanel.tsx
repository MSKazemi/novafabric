// Bypass SoD panel (ADR-0059). Extracted verbatim from SealTab.tsx
// (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api, type SealBypassResponse } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';
import { fmt } from './helpers';

export default function BypassSodPanel({ runIds }: { runIds: string[] }) {
  const [capsuleId, setCapsuleId] = useState('');
  const [durationHours, setDurationHours] = useState(24);
  const [reason, setReason] = useState('');
  const [keyPem, setKeyPem] = useState('');
  const [certPem, setCertPem] = useState('');
  const [targetEnv, setTargetEnv] = useState('production');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<SealBypassResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const inputClass =
    'w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  const reasonLen = reason.length;
  const canSubmit = capsuleId.trim() && reasonLen >= 50 && keyPem.trim() && certPem.trim() && !loading;

  const submit = useCallback(async () => {
    if (!canSubmit) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.sealBypass(capsuleId.trim(), {
        reason,
        duration_hours: durationHours,
        key_pem: keyPem.trim(),
        cert_pem: certPem.trim(),
        target_env: targetEnv,
      });
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [canSubmit, capsuleId, reason, durationHours, keyPem, certPem, targetEnv]);

  const cliCmd = `nova seal bypass --capsule-id ${capsuleId.trim() || '<run_id>'} --duration ${durationHours}h --key admin.pem --justification "${reason.slice(0, 60) || 'Emergency hotfix – authorized by CTO'}"`;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
          Bypass SoD Requirement
        </p>
      </div>
      <p className="text-xs text-[var(--color-text-muted)]">
        Create a time-limited bypass of the maker-checker SoD requirement.
        Max 7 days. The bypass is DSSE-signed and permanently logged in the audit trail.
        Requires justification and signing key. (ADR-0059)
      </p>

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Capsule ID
          </label>
          <SuggestInput
            value={capsuleId}
            onChange={setCapsuleId}
            suggestions={runIds}
            placeholder="run_2024_..."
            className={inputClass}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Duration (hours, max 168)
          </label>
          <input
            type="number"
            min={1}
            max={168}
            value={durationHours}
            onChange={(e) => setDurationHours(Math.max(1, Math.min(168, Number(e.target.value))))}
            className={inputClass}
          />
        </div>
      </div>

      <div className="space-y-1">
        <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)] flex items-center gap-2">
          Justification / Reason
          <span className={clsx('text-[var(--text-2xs)]', reasonLen >= 50 ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]')}>
            {reasonLen}/50 min
          </span>
        </label>
        <textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          rows={3}
          placeholder="Emergency hotfix – authorized by CTO (must be at least 50 characters)"
          className={clsx(inputClass, 'resize-y')}
        />
      </div>

      <div className="space-y-1">
        <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          Target Environment
        </label>
        <select
          value={targetEnv}
          onChange={(e) => setTargetEnv(e.target.value)}
          className={inputClass}
        >
          <option value="production">production</option>
          <option value="staging">staging</option>
          <option value="development">development</option>
        </select>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Private Key PEM (ECDSA P-256)
          </label>
          <textarea
            value={keyPem}
            onChange={(e) => setKeyPem(e.target.value)}
            rows={5}
            placeholder="-----BEGIN EC PRIVATE KEY-----&#10;...&#10;-----END EC PRIVATE KEY-----"
            className={clsx(inputClass, 'resize-y')}
            spellCheck={false}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Certificate PEM (X.509)
          </label>
          <textarea
            value={certPem}
            onChange={(e) => setCertPem(e.target.value)}
            rows={5}
            placeholder="-----BEGIN CERTIFICATE-----&#10;...&#10;-----END CERTIFICATE-----"
            className={clsx(inputClass, 'resize-y')}
            spellCheck={false}
          />
        </div>
      </div>

      <button
        onClick={submit}
        disabled={!canSubmit}
        className={clsx(
          'text-xs font-mono px-4 py-2 rounded border transition-colors',
          canSubmit
            ? 'border-[color-mix(in_oklab,var(--color-status-failure)_60%,transparent)] text-[var(--color-status-failure)] hover:bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)]'
            : 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed',
        )}
      >
        {loading ? 'creating bypass…' : 'Create SoD bypass'}
      </button>

      {error && (
        <p className="text-xs text-[var(--color-status-failure)]">Error: {error}</p>
      )}

      {result && (
        <div className="rounded border border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] p-3 space-y-2">
          <p className="text-xs font-mono font-bold text-[var(--color-status-success)]">
            ✓ Bypass created — permanently logged in audit trail
          </p>
          <div className="grid grid-cols-2 gap-3 text-[11px] font-mono">
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">UUID</p>
              <p className="text-[var(--color-text)] break-all">{result.bypass_uuid}</p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">Authorized by</p>
              <p className="text-[var(--color-text)] truncate">{result.authorized_by}</p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">Valid until</p>
              <p className="text-[var(--color-text-muted)]">{fmt(result.valid_until)}</p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-0.5">Target env</p>
              <p className="text-[var(--color-text-muted)]">{result.target_env}</p>
            </div>
          </div>
        </div>
      )}

      {/* CLI reference */}
      <div className="space-y-1">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">CLI equivalent</p>
        <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <pre className="text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap break-all">
            {cliCmd}
          </pre>
          <div className="absolute top-1.5 right-1.5">
            <CopyButton text={cliCmd} label="CLI" />
          </div>
        </div>
      </div>
    </div>
  );
}
