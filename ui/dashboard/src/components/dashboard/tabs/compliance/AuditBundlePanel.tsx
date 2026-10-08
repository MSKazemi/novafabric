/**
 * Audit Bundle panel (`nova audit bundle`) — exemplar of the
 * PanelScaffold + useMutation pattern. Same API call and rendered result data
 * as the pre-split bespoke panel; only the chrome comes from the scaffold.
 */
import { useCallback, useState } from 'react';
import { api } from '../../../../lib/api';
import { useMutation } from '../../../../lib/useMutation';
import PanelScaffold from '../../PanelScaffold';

const PROFILES = ['nist-ai-rmf', 'eu-ai-act', 'iso-42001'];

export default function AuditBundlePanel() {
  const [profile, setProfile] = useState('nist-ai-rmf');

  const bundle = useMutation(
    () => api.auditBundle(profile),
    { silentSuccess: true, silentError: true },
  );
  const result = bundle.result;

  const download = useCallback(() => {
    if (!result) return;
    const bytes = atob(result.content_base64);
    const arr = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
    const blob = new Blob([arr], { type: 'application/zip' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = result.filename; a.click();
    URL.revokeObjectURL(url);
  }, [result]);

  return (
    <PanelScaffold
      id="audit-bundle"
      title="Audit Bundle Export"
      subtitle={
        <>
          Package audit report + evidence into a ZIP — <code className="font-mono">nova audit bundle</code>
        </>
      }
      cli={`nova audit bundle --profile ${profile} --output audit-bundle.zip`}
      form={
        <label className="block space-y-1">
          <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Profile</span>
          <select
            value={profile}
            onChange={e => setProfile(e.target.value)}
            className="block text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          >
            {PROFILES.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
      }
      onSubmit={() => void bundle.run()}
      submitLabel="Generate bundle"
      pending={bundle.pending}
      error={bundle.error}
    >
      {result && (
        <div className="flex items-center gap-3 rounded border border-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_5%,transparent)] px-3 py-2">
          <span className="text-[var(--color-status-success)] text-sm">↓</span>
          <div className="flex-1 min-w-0">
            <p className="text-xs font-mono text-[var(--color-text)] truncate">{result.filename}</p>
            <p className="text-[10px] text-[var(--color-text-faint)]">
              {(result.size_bytes / 1024).toFixed(1)} KB · score {(result.overall_score * 100).toFixed(1)}% · {result.profile}
            </p>
          </div>
          <button
            onClick={download}
            className="px-2.5 py-1 text-[10px] rounded border border-[var(--color-status-success)] text-[var(--color-status-success)] hover:bg-[color-mix(in_oklab,var(--color-status-success)_15%,transparent)] transition-colors font-mono"
          >download</button>
        </div>
      )}
    </PanelScaffold>
  );
}
