import { useState } from 'react';
import { clsx } from 'clsx';
import { evidenceBundle } from '../../lib/fixtures';
import { verifyEvidenceBundle, type VerifyResult } from '../../lib/ed25519verify';

export default function EvidenceViewer() {
  const [tampered, setTampered] = useState(false);
  const [result, setResult] = useState<VerifyResult | null>(null);
  const [verifying, setVerifying] = useState(false);

  const onVerify = async () => {
    setVerifying(true);
    try {
      const r = await verifyEvidenceBundle(tampered);
      setResult(r);
    } finally {
      setVerifying(false);
    }
  };

  const dsse = evidenceBundle.dsse as unknown as {
    _type: string;
    subject: Array<{ name: string; digest: { sha256: string } }>;
    predicateType: string;
    signature: { keyid: string; alg: string; sig_b64: string; message_hash_sha256: string };
  };
  const manifest = evidenceBundle.manifest as { files: Array<{ path: string; sha256: string; size: number }>; manifest_hash: string };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[1fr_360px] gap-4">
      <div className="space-y-4">
        {/* DSSE Statement */}
        <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)]">
          <header className="px-5 py-3 border-b border-[var(--color-border)] flex items-center justify-between">
            <div>
              <h3 className="text-[var(--color-text)] font-medium text-sm">in-toto DSSE Statement v1</h3>
              <p className="text-xs text-[var(--color-text-faint)] font-mono mt-0.5">{dsse._type}</p>
            </div>
            <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">signed envelope</span>
          </header>
          <div className="p-5 grid md:grid-cols-2 gap-4 text-xs">
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Subject</p>
              <code className="text-[var(--color-text)] font-mono break-all">{dsse.subject[0].name}</code>
              <p className="mt-2 text-[var(--color-text-muted)] break-all font-mono">
                <span className="text-[var(--color-text-faint)]">sha256:</span> {dsse.subject[0].digest.sha256.slice(0, 32)}…
              </p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Predicate type</p>
              <code className="text-[var(--color-text)] font-mono break-all">{dsse.predicateType}</code>
            </div>
            <div className="md:col-span-2 pt-3 border-t border-[var(--color-border)]">
              <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">Signature</p>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[var(--color-text-muted)]">
                <dt>keyid</dt><dd className="text-[var(--color-text)] font-mono">{dsse.signature.keyid}</dd>
                <dt>alg</dt><dd className="text-[var(--color-text)] font-mono">{dsse.signature.alg}</dd>
                <dt>sig (b64)</dt><dd className="text-[var(--color-text)] font-mono break-all">{dsse.signature.sig_b64}</dd>
                <dt>message hash</dt><dd className="text-[var(--color-text)] font-mono break-all">sha256:{dsse.signature.message_hash_sha256}</dd>
              </dl>
            </div>
          </div>
        </section>

        {/* Bundle file tree */}
        <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)]">
          <header className="px-5 py-3 border-b border-[var(--color-border)] flex items-center justify-between">
            <h3 className="text-[var(--color-text)] font-medium text-sm">Bundle contents</h3>
            <span className="text-[10px] text-[var(--color-text-faint)] font-mono">{manifest.files.length} files</span>
          </header>
          <ul className="p-3 text-xs font-mono">
            {manifest.files.map((f) => (
              <li key={f.path} className="flex items-center justify-between gap-3 px-2 py-1.5 rounded hover:bg-[var(--color-bg-sunken)]">
                <span className="text-[var(--color-text-muted)] truncate">
                  <span className="text-[var(--color-text-faint)] mr-1">▸</span>
                  {f.path}
                </span>
                <span className="flex items-center gap-3 shrink-0 text-[var(--color-text-faint)]">
                  <code title={`sha256:${f.sha256}`}>{f.sha256.slice(0, 12)}…</code>
                  <span>{f.size.toLocaleString()}B</span>
                </span>
              </li>
            ))}
          </ul>
          <footer className="px-5 py-3 border-t border-[var(--color-border)] bg-[var(--color-bg-sunken)] text-xs text-[var(--color-text-muted)]">
            <span className="text-[var(--color-text-faint)] mr-2">manifest_hash:</span>
            <code className="text-[var(--color-text)] font-mono break-all">{manifest.manifest_hash}</code>
          </footer>
        </section>
      </div>

      {/* Verify panel */}
      <aside className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-5 sticky top-24 self-start">
        <h3 className="text-[var(--color-text)] font-medium mb-1">Verify in your browser</h3>
        <p className="text-xs text-[var(--color-text-muted)] leading-relaxed mb-4">
          Real ed25519 verification. The browser uses
          <code className="text-[var(--color-text)] mx-1 bg-[var(--color-bg-raised)] px-1.5 py-0.5 rounded text-[10px]">SubtleCrypto.verify</code>
          against the bundled public key — no mocks, no servers.
        </p>

        <button
          type="button"
          onClick={onVerify}
          disabled={verifying}
          className={clsx(
            'w-full inline-flex items-center justify-center gap-2 px-4 py-3 rounded-md text-sm font-medium transition-colors',
            'bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)]',
            verifying && 'opacity-60 cursor-wait',
          )}
        >
          {verifying ? 'Verifying…' : 'Verify signature'}
        </button>

        <label className="mt-4 flex items-start gap-3 cursor-pointer text-xs text-[var(--color-text-muted)]">
          <input
            type="checkbox"
            checked={tampered}
            onChange={(e) => { setTampered(e.target.checked); setResult(null); }}
            className="mt-0.5 accent-[var(--color-status-failure)]"
          />
          <span>
            <span className="block text-[var(--color-text)] font-medium">Tamper</span>
            <span>Flip one byte in the manifest before re-hashing. Verification will fail.</span>
          </span>
        </label>

        <div className="mt-5 pt-5 border-t border-[var(--color-border)]">
          {result === null ? (
            <p className="text-xs text-[var(--color-text-faint)] italic">
              Click <strong className="text-[var(--color-text-muted)]">Verify signature</strong> to run real ed25519 verification.
            </p>
          ) : result.error ? (
            <div className="text-xs text-[var(--color-status-failure)]">
              <p className="font-medium mb-1">Error</p>
              <code className="font-mono break-all text-[10px]">{result.error}</code>
            </div>
          ) : (
            <div className={clsx(
              'rounded p-3 text-xs',
              result.ok
                ? 'bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)]'
                : 'bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)]',
            )}>
              <div className={clsx(
                'flex items-center gap-2 font-medium mb-2',
                result.ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
              )}>
                {result.ok ? '✓ Signature valid' : '✗ Signature invalid'}
                <span className="text-[10px] text-[var(--color-text-faint)] font-normal">{result.durationMs.toFixed(1)}ms</span>
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[var(--color-text-muted)]">
                <dt>keyid</dt><dd className="font-mono text-[var(--color-text)]">{result.keyid}</dd>
                <dt>sig</dt><dd className="font-mono text-[var(--color-text)] break-all">{result.signatureFingerprint}</dd>
                <dt>msg bytes</dt><dd className="font-mono text-[var(--color-text)]">{result.canonicalMessageBytes.toLocaleString()}</dd>
                <dt>msg hash</dt><dd className="font-mono text-[var(--color-text)] break-all text-[10px]">{result.messageHash.slice(0, 24)}…</dd>
              </dl>
              {!result.ok && tampered && (
                <p className="mt-3 pt-3 border-t border-[color-mix(in_oklab,var(--color-status-failure)_20%,transparent)] text-[var(--color-text-muted)]">
                  Verification failed because one byte of the manifest was changed.
                  The signature was made over the original; any mutation breaks it.
                  This is what tamper-evidence looks like.
                </p>
              )}
            </div>
          )}
        </div>

        <p className="mt-5 text-[10px] text-[var(--color-text-faint)] leading-relaxed">
          The keypair on this page is a demo key committed to the showcase
          fixture. Real evidence bundles use a per-deployment ed25519 key
          per ADR-0011 — Sigstore/Fulcio integration is a v0.4.x roadmap item.
        </p>
      </aside>
    </div>
  );
}
