/**
 * DC-6 compare-versions modal: pick two versions of one asset and render the
 * added/removed/changed spec-key diff.
 * Extracted verbatim from the former RegistryTab monolith — behavior frozen.
 */
import type { AssetDiffResult } from '../../../../lib/api';

export default function CompareVersionsDialog({
  assetName,
  versions,
  fromVersion,
  setFromVersion,
  toVersion,
  setToVersion,
  busy,
  result,
  setResult,
  error,
  setError,
  onSubmit,
  onClose,
}: {
  assetName: string;
  versions: string[];
  fromVersion: string;
  setFromVersion: (v: string) => void;
  toVersion: string;
  setToVersion: (v: string) => void;
  busy: boolean;
  result: AssetDiffResult | null;
  setResult: (r: AssetDiffResult | null) => void;
  error: string | null;
  setError: (e: string | null) => void;
  onSubmit: () => void;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="w-full max-w-lg rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-xl p-5 space-y-4">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-sm font-semibold text-[var(--color-text)]">Compare versions — {assetName}</h2>
            <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">CLI equivalent: <code className="font-mono">nova diff {assetName}@{fromVersion} {assetName}@{toVersion}</code></p>
          </div>
          <button
            onClick={onClose}
            className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-lg leading-none"
            aria-label="Close"
          >×</button>
        </div>

        {/* Version selectors */}
        <div className="flex items-center gap-3">
          <label className="flex-1 block">
            <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">From</span>
            <select
              value={fromVersion}
              onChange={e => { setFromVersion(e.target.value); setResult(null); setError(null); }}
              className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-xs"
            >
              {versions.map(v => (
                <option key={v} value={v}>{v}</option>
              ))}
            </select>
          </label>
          <span className="text-[var(--color-text-faint)] mt-4">→</span>
          <label className="flex-1 block">
            <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">To</span>
            <select
              value={toVersion}
              onChange={e => { setToVersion(e.target.value); setResult(null); setError(null); }}
              className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono text-xs"
            >
              {versions.map(v => (
                <option key={v} value={v}>{v}</option>
              ))}
            </select>
          </label>
        </div>

        <button
          onClick={onSubmit}
          disabled={busy || fromVersion === toVersion}
          className="w-full py-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-xs font-medium text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {busy ? 'Comparing…' : 'Compare'}
        </button>

        {/* Error */}
        {error && (
          <p className="text-[11px] text-[var(--color-status-failure)]">{error}</p>
        )}

        {/* Diff result */}
        {result && (
          <div className="space-y-2">
            {result.identical ? (
              <p className="text-[11px] text-[var(--color-status-success)] font-medium">No differences — specs are identical.</p>
            ) : (
              <div className="rounded border border-[var(--color-border)] overflow-hidden">
                <table className="w-full text-[10px] font-mono">
                  <thead className="bg-[var(--color-bg-sunken)] border-b border-[var(--color-border)]">
                    <tr>
                      <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">Key</th>
                      <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">From ({fromVersion})</th>
                      <th className="text-left px-3 py-1.5 text-[var(--color-text-faint)] font-medium uppercase tracking-wider">To ({toVersion})</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--color-border)]">
                    {result.changed.map(r => (
                      <tr key={`chg-${r.key}`} className="bg-[color-mix(in_oklab,var(--color-status-pending)_6%,transparent)]">
                        <td className="px-3 py-1.5 text-[var(--color-text-muted)]">
                          <span className="text-[var(--color-status-pending)] mr-1">~</span>{r.key}
                        </td>
                        <td className="px-3 py-1.5 text-[var(--color-status-failure)] line-through opacity-70">{JSON.stringify(r.from)}</td>
                        <td className="px-3 py-1.5 text-[var(--color-status-success)]">{JSON.stringify(r.to)}</td>
                      </tr>
                    ))}
                    {result.added.map(r => (
                      <tr key={`add-${r.key}`} className="bg-[color-mix(in_oklab,var(--color-status-success)_6%,transparent)]">
                        <td className="px-3 py-1.5 text-[var(--color-text-muted)]">
                          <span className="text-[var(--color-status-success)] mr-1">+</span>{r.key}
                        </td>
                        <td className="px-3 py-1.5 text-[var(--color-text-faint)]">—</td>
                        <td className="px-3 py-1.5 text-[var(--color-status-success)]">{JSON.stringify(r.value)}</td>
                      </tr>
                    ))}
                    {result.removed.map(r => (
                      <tr key={`rem-${r.key}`} className="bg-[color-mix(in_oklab,var(--color-status-failure)_6%,transparent)]">
                        <td className="px-3 py-1.5 text-[var(--color-text-muted)]">
                          <span className="text-[var(--color-status-failure)] mr-1">-</span>{r.key}
                        </td>
                        <td className="px-3 py-1.5 text-[var(--color-status-failure)]">{JSON.stringify(r.value)}</td>
                        <td className="px-3 py-1.5 text-[var(--color-text-faint)]">—</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
