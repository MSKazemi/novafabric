import { useState, useEffect } from 'react';
import { api, getConnection } from '../../lib/api';
import type { EvidenceDetail } from '../../lib/api';

interface VerifyState {
  // The verdict of the server's verifier (POST /api/evidence/{id}/verify) -- the same
  // recipe as `nova verify <bundle>.zip`. The browser never verifies on its own: a
  // single DSSE signature check here would ignore every member digest.
  status: 'idle' | 'running' | 'pass' | 'warn' | 'fail';
  message: string;
  details: string[];
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function copyToClipboard(text: string): void {
  navigator.clipboard.writeText(text).catch(() => { /* ignore */ });
}

interface Props {
  bundleId: string;
  onBack: () => void;
}

export default function EvidenceDetailPanel({ bundleId, onBack }: Props) {
  const [detail, setDetail] = useState<EvidenceDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [verify, setVerify] = useState<VerifyState>({ status: 'idle', message: '', details: [] });
  const [showFiles, setShowFiles] = useState(false);

  useEffect(() => {
    setLoading(true);
    setError(null);
    setVerify({ status: 'idle', message: '', details: [] });
    api.getEvidenceDetail(bundleId)
      .then(setDetail)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [bundleId]);

  async function handleVerify() {
    if (!detail) return;
    setVerify({ status: 'running', message: '', details: [] });
    try {
      const r = await api.verifyEvidence(bundleId);
      const unsigned = r.unsigned_members ?? [];
      if (!r.valid) {
        setVerify({
          status: 'fail',
          message: 'Verification FAILED — this bundle may have been tampered with.',
          details: r.errors ?? [],
        });
      } else if (r.members_signed === false) {
        setVerify({
          status: 'warn',
          message: `Signed content verifies, but ${unsigned.length} member(s) are not covered by any signature (legacy bundle).`,
          details: unsigned,
        });
      } else if (r.signer_trust === 'anchored') {
        setVerify({
          status: 'pass',
          message: 'Every member verifies and the signer is an operator-trusted key.',
          details: r.signer_fingerprints ?? [],
        });
      } else {
        setVerify({
          status: 'warn',
          message: 'The bundle is consistent, but its signer is not pinned (no evidence trust list) — this proves consistency, not who signed it.',
          details: r.signer_fingerprints ?? [],
        });
      }
    } catch (e) {
      setVerify({ status: 'fail', message: `Verification error: ${e instanceof Error ? e.message : String(e)}`, details: [] });
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-[var(--color-text-faint)] text-sm">
        Loading…
      </div>
    );
  }

  if (error || !detail) {
    return (
      <div className="p-4 text-sm text-[var(--color-status-failure)]">
        {error ?? 'Bundle not found.'}
      </div>
    );
  }

  const { base, token } = getConnection();
  const downloadUrl = `${base}/api/evidence/${encodeURIComponent(bundleId)}/download?token=${encodeURIComponent(token ?? '')}`;

  return (
    <div className="flex flex-col h-full overflow-auto">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-2 border-b border-[var(--color-border)] shrink-0">
        <button
          onClick={onBack}
          className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors"
        >
          ← Bundles
        </button>
        <span className="text-[var(--color-border)]">|</span>
        <code className="text-xs font-mono text-[var(--color-text-faint)] truncate" title={detail.bundle_id}>
          {detail.bundle_id}
        </code>
        <a
          href={downloadUrl}
          download={`evidence-${detail.bundle_id}.zip`}
          className="ml-auto text-xs px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)] transition-colors"
        >
          ↓ Download
        </a>
      </div>

      <div className="p-4 space-y-4 flex-1">
        {/* Meta */}
        <div className="grid grid-cols-3 gap-3 text-xs">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Run ID</div>
            <code className="font-mono text-[var(--color-text)]" title={detail.run_id}>
              {detail.run_id.length > 24 ? `${detail.run_id.slice(0, 24)}…` : detail.run_id}
            </code>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Size</div>
            <span>{formatBytes(detail.size_bytes)}</span>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Created</div>
            <span>{detail.timestamp ? new Date(detail.timestamp).toLocaleString() : '—'}</span>
          </div>
        </div>

        {/* Verify section */}
        <div className="border border-[var(--color-border)] rounded p-3 space-y-2">
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-semibold">
            Cryptographic Verification
          </div>

          {/* Signing key fingerprint: the key whose signature verified, per the server. */}
          {detail.signing_key_fingerprint ? (
            <div className="flex items-center gap-2 text-xs">
              <span className="text-[var(--color-text-faint)]">Signer key:</span>
              <code
                className="font-mono bg-[var(--color-bg-raised)] px-1.5 py-0.5 rounded text-[var(--color-text)]"
                title="SHA-256 of the public key whose signature verified"
              >
                {detail.signing_key_fingerprint}
              </code>
              <button
                onClick={() => copyToClipboard(detail.signing_key_fingerprint!)}
                className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-[10px]"
                title="Copy fingerprint"
              >
                ⎘
              </button>
            </div>
          ) : (
            <p className="text-xs text-[var(--color-text-faint)]">No signature verified for this bundle.</p>
          )}

          {verify.status === 'pass' && (
            <div className="rounded p-2 bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)] text-[var(--color-status-success)] text-xs">
              ✓ {verify.message}
            </div>
          )}
          {verify.status === 'warn' && (
            <div className="rounded p-2 bg-[color-mix(in_oklab,var(--color-status-pending)_12%,transparent)] text-[var(--color-status-pending)] text-xs">
              ⚠ {verify.message}
            </div>
          )}
          {verify.status === 'fail' && (
            <div className="rounded p-2 bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)] text-[var(--color-status-failure)] text-xs">
              ✗ {verify.message}
            </div>
          )}
          {verify.details.length > 0 && (
            <ul className="text-[11px] font-mono text-[var(--color-text-muted)] list-disc pl-5 max-h-32 overflow-auto">
              {verify.details.map((d) => <li key={d}>{d}</li>)}
            </ul>
          )}

          {(verify.status === 'idle' || verify.status === 'running') && (
            <button
              onClick={handleVerify}
              disabled={verify.status === 'running'}
              className="text-xs px-3 py-1.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {verify.status === 'running' ? 'Verifying…' : '⊛ Verify bundle'}
            </button>
          )}
        </div>

        {/* DSSE statement */}
        {Object.keys(detail.dsse_statement).length > 0 && (
          <div className="space-y-1">
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-semibold">
              DSSE Statement
            </div>
            <pre className="bg-[var(--color-bg-raised)] p-3 rounded text-[11px] font-mono overflow-auto max-h-48 text-[var(--color-text)]">
              {JSON.stringify(detail.dsse_statement, null, 2)}
            </pre>
          </div>
        )}

        {/* Files */}
        <details
          open={showFiles}
          onToggle={(e) => setShowFiles((e.target as HTMLDetailsElement).open)}
        >
          <summary className="text-xs text-[var(--color-text-muted)] cursor-pointer hover:text-[var(--color-text)] select-none">
            {detail.files.length} files in bundle
          </summary>
          <div className="mt-2 space-y-px">
            {detail.files.map((f) => (
              <div key={f.path} className="flex items-center justify-between text-[11px] font-mono text-[var(--color-text-faint)] py-0.5">
                <span className="truncate">{f.path}</span>
                <span className="ml-4 shrink-0">{formatBytes(f.size_bytes)}</span>
              </div>
            ))}
          </div>
        </details>
      </div>
    </div>
  );
}
