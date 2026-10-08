import { useState, useEffect } from 'react';
import { api, getConnection } from '../../lib/api';
import type { EvidenceDetail } from '../../lib/api';

interface VerifyState {
  status: 'idle' | 'running' | 'pass' | 'fail' | 'keyless' | 'no-subtle-crypto';
  message: string;
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
  const [verify, setVerify] = useState<VerifyState>({ status: 'idle', message: '' });
  const [showFiles, setShowFiles] = useState(false);

  useEffect(() => {
    setLoading(true);
    setError(null);
    setVerify({ status: 'idle', message: '' });
    api.getEvidenceDetail(bundleId)
      .then(setDetail)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [bundleId]);

  async function handleVerify() {
    if (!detail) return;
    if (!window.crypto?.subtle) {
      setVerify({ status: 'no-subtle-crypto', message: 'SubtleCrypto requires a secure context (HTTPS or localhost). Use cosign verify-blob instead.' });
      return;
    }
    if (!detail.signing_key_fingerprint) {
      setVerify({ status: 'keyless', message: '' });
      return;
    }

    setVerify({ status: 'running', message: '' });

    try {
      const { base, token } = getConnection();
      // Fetch the raw ZIP
      const resp = await fetch(`${base}/api/evidence/${encodeURIComponent(bundleId)}/download?token=${encodeURIComponent(token ?? '')}`);
      if (!resp.ok) throw new Error(`Download failed: ${resp.status}`);
      const zipBytes = new Uint8Array(await resp.arrayBuffer());

      // Simple ZIP local-file-header scan to extract stored (method=0) entries by name
      const findZipEntry = (name: string): Uint8Array | null => {
        const enc = new TextEncoder();
        const nameBytes = enc.encode(name);
        for (let i = 0; i < zipBytes.length - 30; i++) {
          if (zipBytes[i] === 0x50 && zipBytes[i + 1] === 0x4b &&
              zipBytes[i + 2] === 0x03 && zipBytes[i + 3] === 0x04) {
            const compMethod = zipBytes[i + 8] | (zipBytes[i + 9] << 8);
            const fnLen = zipBytes[i + 26] | (zipBytes[i + 27] << 8);
            const extraLen = zipBytes[i + 28] | (zipBytes[i + 29] << 8);
            const fn = zipBytes.slice(i + 30, i + 30 + fnLen);
            if (fn.length === nameBytes.length && fn.every((b, j) => b === nameBytes[j])) {
              if (compMethod !== 0) return null; // deflate — not extractable here
              const compSize = zipBytes[i + 18] | (zipBytes[i + 19] << 8) |
                               (zipBytes[i + 20] << 16) | (zipBytes[i + 21] << 24);
              const dataOffset = i + 30 + fnLen + extraLen;
              return zipBytes.slice(dataOffset, dataOffset + compSize);
            }
          }
        }
        return null;
      };

      // Get cert bytes
      const certPath = 'signatures/run.intoto.json.cert';
      const certBytes = findZipEntry(certPath);
      if (!certBytes) {
        setVerify({ status: 'keyless', message: '' });
        return;
      }

      // Import public key from PEM — Ed25519 SubjectPublicKeyInfo DER
      const pemStr = new TextDecoder().decode(certBytes);
      const b64 = pemStr.replace(/-----[^-]+-----/g, '').replace(/\s+/g, '');
      const derBytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
      // Ed25519 SubjectPublicKeyInfo: last 32 bytes are the raw key
      const rawKey = derBytes.slice(derBytes.length - 32);
      const cryptoKey = await window.crypto.subtle.importKey(
        'raw', rawKey, { name: 'Ed25519' }, false, ['verify'],
      );

      // Build DSSE PAE message
      const envelope = detail.dsse_envelope as Record<string, unknown>;
      const payloadType = (envelope.payloadType as string) ?? 'application/vnd.in-toto+json';
      const payloadB64 = (envelope.payload as string) ?? '';
      const payloadBytes = Uint8Array.from(atob(payloadB64 + '=='), c => c.charCodeAt(0));
      const enc = new TextEncoder();
      const paeType = enc.encode(payloadType);
      const paeHeader = enc.encode(`DSSEv1 ${paeType.length} `);
      const paeMiddle = enc.encode(` ${payloadBytes.length} `);
      const pae = new Uint8Array(paeHeader.length + paeType.length + paeMiddle.length + payloadBytes.length);
      let off = 0;
      pae.set(paeHeader, off); off += paeHeader.length;
      pae.set(paeType, off); off += paeType.length;
      pae.set(paeMiddle, off); off += paeMiddle.length;
      pae.set(payloadBytes, off);

      // Get signature
      const sigs = (envelope.signatures as Array<Record<string, string>>) ?? [];
      if (!sigs[0]?.sig) throw new Error('No signature found in DSSE envelope');
      const sigBytes = Uint8Array.from(atob(sigs[0].sig + '=='), c => c.charCodeAt(0));

      const valid = await window.crypto.subtle.verify('Ed25519', cryptoKey, sigBytes, pae);
      if (valid) {
        setVerify({ status: 'pass', message: `Signed by key [${detail.signing_key_fingerprint}]` });
      } else {
        setVerify({ status: 'fail', message: 'Signature INVALID — this bundle may have been tampered with.' });
      }
    } catch (e) {
      setVerify({ status: 'fail', message: `Verification error: ${e instanceof Error ? e.message : String(e)}` });
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
  const cosignCmd = `cosign verify-blob \\\n  --bundle signatures/run.intoto.json.bundle \\\n  attestations/run.intoto.json`;

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

          {/* Signing key fingerprint */}
          {detail.signing_key_fingerprint ? (
            <div className="flex items-center gap-2 text-xs">
              <span className="text-[var(--color-text-faint)]">Key fingerprint:</span>
              <code
                className="font-mono bg-[var(--color-bg-raised)] px-1.5 py-0.5 rounded text-[var(--color-text)]"
                title="First 16 hex chars of SHA-256(public key bytes)"
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
            <p className="text-xs text-[var(--color-text-faint)]">No local signing key found — keyless Sigstore bundle.</p>
          )}

          {/* Verify result banner */}
          {verify.status === 'pass' && (
            <div className="rounded p-2 bg-[color-mix(in_oklab,var(--color-status-success)_12%,transparent)] text-[var(--color-status-success)] text-xs">
              ✓ Signature valid — {verify.message}
            </div>
          )}
          {verify.status === 'fail' && (
            <div className="rounded p-2 bg-[color-mix(in_oklab,var(--color-status-failure)_12%,transparent)] text-[var(--color-status-failure)] text-xs">
              ✗ {verify.message}
            </div>
          )}
          {verify.status === 'no-subtle-crypto' && (
            <div className="rounded p-2 bg-[color-mix(in_oklab,var(--color-status-pending)_12%,transparent)] text-[var(--color-status-pending)] text-xs">
              ⚠ {verify.message}
            </div>
          )}
          {verify.status === 'keyless' && (
            <div className="text-xs text-[var(--color-text-muted)] space-y-1">
              <p>Keyless Sigstore bundle — browser verification is not applicable.</p>
              <p>To verify:</p>
              <div className="relative">
                <pre className="bg-[var(--color-bg-raised)] p-2 rounded font-mono text-[11px] overflow-x-auto">
                  {cosignCmd}
                </pre>
                <button
                  onClick={() => copyToClipboard(cosignCmd)}
                  className="absolute top-1 right-1 text-[10px] text-[var(--color-text-faint)] hover:text-[var(--color-text)] bg-[var(--color-bg-raised)] px-1 rounded"
                  title="Copy cosign command"
                >
                  ⎘
                </button>
              </div>
            </div>
          )}

          {/* Verify button */}
          {(verify.status === 'idle' || verify.status === 'running') && detail.signing_key_fingerprint && (
            <button
              onClick={handleVerify}
              disabled={verify.status === 'running'}
              className="text-xs px-3 py-1.5 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {verify.status === 'running' ? 'Verifying…' : '⊛ Verify signature'}
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
