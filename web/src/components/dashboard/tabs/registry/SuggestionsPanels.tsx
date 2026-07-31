/**
 * C-2 suggestion-engine surfaces: the compact banner (registry non-empty),
 * the smart empty state (registry empty but runs exist), and the dedicated
 * `nova suggest-register` panel.
 * Extracted verbatim from the former RegistryTab monolith — behavior frozen.
 */
import { useState, useEffect, useRef } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import type { RegistrationSuggestion } from '../../../../lib/api';

// C-2 — Compact banner shown when there are unregistered detections alongside existing assets.
export function SuggestionsBanner({
  suggestions,
  onDraftOpen,
}: {
  suggestions: RegistrationSuggestion[];
  onDraftOpen: (s: RegistrationSuggestion) => void;
}) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-4 py-3 space-y-2">
      <p className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">
        {suggestions.length} unregistered asset{suggestions.length > 1 ? 's' : ''} detected in recent runs
      </p>
      <div className="flex flex-wrap gap-1.5">
        {suggestions.map((s, i) => (
          <button
            key={i}
            type="button"
            onClick={() => onDraftOpen(s)}
            title={`${Math.round(s.confidence * 100)}% confidence · ${s.call_count} calls`}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] text-[var(--color-text-muted)] text-[10px] font-mono transition-colors"
          >
            <span className="text-[var(--color-text-faint)] uppercase tracking-wider">{s.asset_type}</span>
            <span className="text-[var(--color-text)]">{s.detected_name}</span>
            <span className="text-[var(--color-text-faint)]">{Math.round(s.confidence * 100)}%</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// C-2 — Smart empty state shown when registry is empty but runs exist.
export function SuggestionsEmptyState({
  suggestions,
  onDraftOpen,
  onRegisterOpen,
}: {
  suggestions: RegistrationSuggestion[] | null;
  onDraftOpen: (s: RegistrationSuggestion) => void;
  onRegisterOpen: () => void;
}) {
  const hasSuggestions = suggestions !== null && suggestions.length > 0;
  const byType = suggestions?.reduce<Record<string, number>>((acc, s) => {
    acc[s.asset_type] = (acc[s.asset_type] ?? 0) + 1;
    return acc;
  }, {}) ?? {};
  const typesSummary = Object.entries(byType)
    .map(([t, n]) => `${n} ${t}${n > 1 ? 's' : ''}`)
    .join('  ·  ');

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-8 space-y-4">
      <div className="text-center space-y-1">
        <p className="text-sm text-[var(--color-text-muted)]">No assets registered.</p>
        {hasSuggestions && (
          <p className="text-xs text-[var(--color-text-faint)]">
            {suggestions!.length} unregistered asset{suggestions!.length > 1 ? 's' : ''} detected in recent runs
            {typesSummary ? ` — ${typesSummary}` : ''}
          </p>
        )}
        {suggestions === null && (
          <p className="text-[10px] text-[var(--color-text-faint)] animate-pulse">Scanning recent runs…</p>
        )}
        {suggestions !== null && suggestions.length === 0 && (
          <p className="text-[10px] text-[var(--color-text-faint)]">
            Use Register above or run <code className="font-mono">nova register &lt;spec.yaml&gt;</code>
          </p>
        )}
      </div>

      {hasSuggestions && (
        <div className="rounded border border-[var(--color-border)] overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-[var(--color-bg-raised)] border-b border-[var(--color-border)]">
              <tr>
                <th className="text-left px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Type</th>
                <th className="text-left px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Name</th>
                <th className="text-right px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Calls</th>
                <th className="text-right px-3 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-medium">Confidence</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--color-border)]">
              {suggestions!.map((s, i) => (
                <tr key={i} className="hover:bg-[var(--color-bg-raised)] transition-colors">
                  <td className="px-3 py-2 font-mono text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider">{s.asset_type}</td>
                  <td className="px-3 py-2 font-mono text-[var(--color-text)]">{s.detected_name}</td>
                  <td className="px-3 py-2 text-right text-[var(--color-text-faint)]">{s.call_count}</td>
                  <td className="px-3 py-2 text-right text-[var(--color-text-faint)]">{Math.round(s.confidence * 100)}%</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => onDraftOpen(s)}
                      className="px-2 py-1 rounded border border-[var(--color-border)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)] text-[var(--color-text-muted)] text-[10px] uppercase tracking-wider font-medium transition-colors"
                    >register →</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex items-center justify-center gap-3 pt-1">
        <button
          onClick={onRegisterOpen}
          className="px-3 py-1.5 rounded-md text-xs font-medium border border-[var(--color-border)] hover:border-[var(--color-border-strong)] bg-[var(--color-bg-raised)] hover:bg-[var(--color-bg)] text-[var(--color-text)]"
        >+ Register asset manually</button>
      </div>
    </div>
  );
}

// ── SuggestRegisterPanel — dedicated panel for `nova suggest-register` ────────

export function SuggestRegisterPanel({
  suggestions,
  onRefresh,
}: {
  suggestions: RegistrationSuggestion[] | null;
  onRefresh: () => void;
}) {
  const [registering, setRegistering] = useState<string | null>(null);
  const [flash, setFlash] = useState<{ name: string; ok: boolean } | null>(null);
  const flashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (flashTimerRef.current) clearTimeout(flashTimerRef.current); }, []);

  const doRegister = async (s: RegistrationSuggestion) => {
    const key = `${s.asset_type}:${s.detected_name}`;
    setRegistering(key);
    try {
      await api.registerFromSuggestion(s.draft_spec_yaml);
      setFlash({ name: s.detected_name, ok: true });
      onRefresh();
    } catch {
      setFlash({ name: s.detected_name, ok: false });
    } finally {
      setRegistering(null);
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
      flashTimerRef.current = setTimeout(() => setFlash(null), 2500);
    }
  };

  const total = suggestions?.length ?? 0;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-faint)]">
            Suggest Register
          </p>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Assets detected in recent capsules not yet in the registry.
          </p>
        </div>
        <button
          onClick={onRefresh}
          className="text-[10px] font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-faint)] hover:text-[var(--color-text)] hover:border-[var(--color-border-strong)] transition-colors"
        >
          ↺ refresh
        </button>
      </div>

      {flash && (
        <p className={clsx(
          'text-[10px] font-mono',
          flash.ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
        )}>
          {flash.ok ? `✓ registered ${flash.name}` : `✗ failed to register ${flash.name}`}
        </p>
      )}

      {suggestions === null ? (
        <p className="text-[10px] text-[var(--color-text-faint)] italic">Scanning capsules…</p>
      ) : total === 0 ? (
        <p className="text-[10px] text-[var(--color-status-success)]">✓ No unregistered assets detected.</p>
      ) : (
        <div className="space-y-1.5">
          {suggestions.map((s) => {
            const key = `${s.asset_type}:${s.detected_name}`;
            const busy = registering === key;
            return (
              <div key={key} className="flex items-center gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5">
                <div className="flex-1 min-w-0 space-y-px">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[var(--text-2xs)] uppercase tracking-wider text-[var(--color-text-faint)] font-mono">{s.asset_type}</span>
                    <span className="text-xs font-mono text-[var(--color-text)] truncate">{s.detected_name}</span>
                    {s.detected_version && (
                      <span className="text-[10px] text-[var(--color-text-faint)]">{s.detected_version}</span>
                    )}
                  </div>
                  <div className="text-[var(--text-2xs)] text-[var(--color-text-faint)]">
                    {s.call_count} calls · {Math.round(s.confidence * 100)}% confidence
                    {s.warnings?.length > 0 && (
                      <span className="text-[var(--color-status-pending)] ml-1">⚠ {s.warnings[0]}</span>
                    )}
                  </div>
                </div>
                <button
                  onClick={() => void doRegister(s)}
                  disabled={busy}
                  className={clsx(
                    'text-[10px] font-mono px-2 py-1 rounded border shrink-0 transition-colors',
                    busy
                      ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
                      : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
                  )}
                >
                  {busy ? '…' : 'Register'}
                </button>
              </div>
            );
          })}
        </div>
      )}

      <p className="text-[10px] font-mono text-[var(--color-text-faint)]">
        CLI: <code>nova suggest-register</code> · <code>nova suggest-register --auto --min-confidence 0.8</code>
      </p>
    </div>
  );
}
