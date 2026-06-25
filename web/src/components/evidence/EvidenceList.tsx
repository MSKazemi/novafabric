import { useState, useCallback } from 'react';
import { clsx } from 'clsx';
import { api, getConnection } from '../../lib/api';
import type { EvidenceSummary, VerifyResult } from '../../lib/api';
import type { Tab } from '../dashboard/Sidebar';
import EmptyState from '../ui/EmptyState';

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function relativeTime(iso: string): string {
  if (!iso) return '—';
  const diffMs = Date.now() - new Date(iso).getTime();
  const diffMin = Math.floor(diffMs / 60_000);
  if (diffMin < 2) return 'just now';
  if (diffMin < 60) return `${diffMin} min ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

function CheckBadge({ label, ok }: { label: string; ok: boolean | null }) {
  if (ok === null) {
    return (
      <span className="inline-flex items-center gap-0.5 px-1 py-0.5 rounded text-[9px] font-mono bg-[var(--color-bg-raised)] text-[var(--color-text-faint)] border border-[var(--color-border)]">
        {label} –
      </span>
    );
  }
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-0.5 px-1 py-0.5 rounded text-[9px] font-mono border',
        ok
          ? 'bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)] text-[var(--color-status-success)] border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)]'
          : 'bg-[color-mix(in_oklab,var(--color-status-failure)_10%,transparent)] text-[var(--color-status-failure)] border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)]',
      )}
    >
      {label} {ok ? '✓' : '✗'}
    </span>
  );
}

interface Props {
  bundles: EvidenceSummary[];
  selectedId: string | null;
  onSelect: (bundleId: string) => void;
  onRefresh: () => void;
  onNavigate?: (tab: Tab) => void;
}

export default function EvidenceList({ bundles, selectedId, onSelect, onRefresh, onNavigate }: Props) {
  const { token, base } = getConnection();
  const [verifyResults, setVerifyResults] = useState<Record<string, VerifyResult>>({});
  const [verifying, setVerifying] = useState<Set<string>>(new Set());

  const handleVerify = useCallback(async (bundleId: string) => {
    setVerifying((prev) => new Set(prev).add(bundleId));
    try {
      const result = await api.verifyEvidence(bundleId);
      setVerifyResults((prev) => ({ ...prev, [bundleId]: result }));
    } catch (e) {
      // Surface error as a failed verify result so the user sees it
      setVerifyResults((prev) => ({
        ...prev,
        [bundleId]: {
          bundle_id: bundleId,
          run_id: bundleId,
          valid: false,
          signature_ok: false,
          timestamp_ok: null,
          log_integrity_ok: null,
          seal_available: false,
          errors: [e instanceof Error ? e.message : String(e)],
        },
      }));
    } finally {
      setVerifying((prev) => {
        const next = new Set(prev);
        next.delete(bundleId);
        return next;
      });
    }
  }, []);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-[var(--color-border)] shrink-0">
        <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-semibold">
          Evidence Bundles
        </span>
        <div className="flex items-center gap-2">
          {onNavigate && (
            <button
              onClick={() => onNavigate('commands')}
              className="text-[10px] px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)] transition-colors"
            >
              ↗ Export evidence
            </button>
          )}
          <button
            onClick={onRefresh}
            title="Refresh evidence list (r)"
            className="text-[10px] px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-bg-raised)] transition-colors"
          >
            ↺ Refresh
          </button>
        </div>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto">
        {bundles.length === 0 ? (
          <EmptyState
            message="No evidence bundles found."
            cliCommand="nova export-evidence <capsule>"
            variant="fill"
          />
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-[var(--color-border)]">
                <th className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium w-8">
                  Status
                </th>
                <th className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">
                  Run ID
                </th>
                <th className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium w-36">
                  Created
                </th>
                <th className="px-3 py-2 text-right text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium w-20">
                  Size
                </th>
                <th className="px-3 py-2 text-left text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium w-40">
                  Integrity
                </th>
                <th className="px-3 py-2 w-8" />
              </tr>
            </thead>
            <tbody>
              {bundles.map((b) => {
                const isSelected = b.bundle_id === selectedId;
                const downloadUrl = `${base}/api/evidence/${encodeURIComponent(b.bundle_id)}/download?token=${encodeURIComponent(token ?? '')}`;
                const isVerifying = verifying.has(b.bundle_id);
                const result = verifyResults[b.bundle_id];
                return (
                  <tr
                    key={b.bundle_id}
                    onClick={() => onSelect(b.bundle_id)}
                    className={clsx(
                      'border-b border-[var(--color-border)] cursor-pointer transition-colors',
                      isSelected
                        ? 'bg-[var(--color-bg-raised)] border-l-2 border-l-[var(--color-accent)]'
                        : 'hover:bg-[color-mix(in_oklab,var(--color-bg-raised)_60%,transparent)]',
                    )}
                  >
                    <td className="px-3 py-2.5 text-center">
                      <span
                        title="Manifest integrity check — SHA-256 of manifest.json matches its own manifest_hash field. Not a full signature verify."
                        className={clsx(
                          'text-sm font-bold',
                          b.verified ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
                        )}
                      >
                        {b.verified ? '✓' : '✗'}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 font-mono" title={b.run_id}>
                      {b.run_id.length > 20 ? `${b.run_id.slice(0, 20)}…` : b.run_id}
                    </td>
                    <td className="px-3 py-2.5 text-[var(--color-text-muted)]" title={b.timestamp}>
                      {relativeTime(b.timestamp)}
                    </td>
                    <td className="px-3 py-2.5 text-right text-[var(--color-text-faint)] font-mono">
                      {formatBytes(b.size_bytes)}
                    </td>
                    <td
                      className="px-3 py-2.5"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {result ? (
                        <div className="flex items-center gap-1 flex-wrap">
                          <CheckBadge label="sig" ok={result.signature_ok} />
                          <CheckBadge label="tsr" ok={result.timestamp_ok} />
                          <CheckBadge label="log" ok={result.log_integrity_ok} />
                        </div>
                      ) : (
                        <button
                          onClick={() => handleVerify(b.bundle_id)}
                          disabled={isVerifying}
                          title="Run full cryptographic verification (DSSE signature + RFC 3161 TSR + Merkle log)"
                          className={clsx(
                            'text-[10px] px-2 py-0.5 rounded border transition-colors',
                            isVerifying
                              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-wait'
                              : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-accent)] hover:border-[var(--color-accent)]',
                          )}
                        >
                          {isVerifying ? '…' : 'Verify'}
                        </button>
                      )}
                    </td>
                    <td
                      className="px-3 py-2.5 text-center"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <a
                        href={downloadUrl}
                        download={`evidence-${b.bundle_id}.zip`}
                        title="Download bundle"
                        className="text-[var(--color-text-faint)] hover:text-[var(--color-accent)] transition-colors text-sm"
                      >
                        ↓
                      </a>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
