// KG alias management (nova kg alias list/register, Tier-2 alias table).
// Extracted verbatim from KGTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import EmptyState from '../../../ui/EmptyState';
import { inputClass, labelClass } from './shared';

export default function KGAliasPanel() {
  const [canonical, setCanonical] = useState('');
  const [loading, setLoading] = useState(false);
  const [aliases, setAliases] = useState<Array<{
    alias: string; canonical: string; entity_type: string; confidence: number; source: string; created_at: string;
  }> | null>(null);

  const [regAlias, setRegAlias] = useState('');
  const [regCanonical, setRegCanonical] = useState('');
  const [regEntityType, setRegEntityType] = useState('');
  const [regResult, setRegResult] = useState<{ ok: boolean; error?: string } | null>(null);
  const [regLoading, setRegLoading] = useState(false);

  const handleList = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.kgAliasList(canonical.trim() || undefined);
      setAliases(r.aliases);
    } finally {
      setLoading(false);
    }
  }, [canonical]);

  const handleRegister = useCallback(async () => {
    if (!regAlias.trim() || !regCanonical.trim() || !regEntityType.trim()) return;
    setRegLoading(true);
    setRegResult(null);
    try {
      const r = await api.kgAliasRegister(regAlias.trim(), regCanonical.trim(), regEntityType.trim());
      setRegResult(r);
      if (r.ok) {
        setRegAlias('');
        setRegCanonical('');
        setRegEntityType('');
        handleList();
      }
    } catch (e) {
      setRegResult({ ok: false, error: (e as Error).message });
    } finally {
      setRegLoading(false);
    }
  }, [regAlias, regCanonical, regEntityType, handleList]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div>
        <h3 className="text-xs font-semibold text-[var(--color-text)]">KG Alias Management</h3>
        <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
          nova kg alias list/register — Tier-2 alias table: map short names to canonical entity IDs
        </p>
      </div>

      <div className="space-y-2">
        <p className={labelClass}>List Aliases</p>
        <div className="flex gap-2">
          <input
            type="text"
            value={canonical}
            onChange={(e) => setCanonical(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleList()}
            placeholder="Filter by canonical (optional)"
            className={`flex-1 ${inputClass}`}
          />
          <button
            onClick={handleList}
            disabled={loading}
            className={clsx(
              'text-xs font-mono px-3 py-1.5 rounded border transition-colors',
              loading
                ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
                : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
            )}
          >
            {loading ? '…' : 'List'}
          </button>
        </div>
        {aliases !== null && (
          aliases.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[var(--color-border)] text-[var(--color-text-faint)]">
                    <th className="text-left py-1 pr-3">alias</th>
                    <th className="text-left py-1 pr-3">canonical</th>
                    <th className="text-left py-1 pr-3">type</th>
                    <th className="text-left py-1">confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {aliases.map((a) => (
                    <tr key={`${a.alias}/${a.entity_type}`} className="border-b border-[var(--color-border)] last:border-0">
                      <td className="py-1 pr-3 text-[var(--color-text)]">{a.alias}</td>
                      <td className="py-1 pr-3 text-[var(--color-text-muted)]">{a.canonical}</td>
                      <td className="py-1 pr-3 text-[var(--color-text-faint)]">{a.entity_type}</td>
                      <td className="py-1 text-[var(--color-text-faint)]">{a.confidence.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState message="No aliases found. Register one below." />
          )
        )}
      </div>

      <div className="space-y-2 border-t border-[var(--color-border)] pt-3">
        <p className={labelClass}>Register Alias</p>
        <div className="grid grid-cols-3 gap-2">
          <input type="text" value={regAlias} onChange={(e) => setRegAlias(e.target.value)} placeholder="alias (gpt4)" className={inputClass} />
          <input type="text" value={regCanonical} onChange={(e) => setRegCanonical(e.target.value)} placeholder="canonical (openai/gpt-4)" className={inputClass} />
          <input type="text" value={regEntityType} onChange={(e) => setRegEntityType(e.target.value)} placeholder="entity_type (model)" className={inputClass} />
        </div>
        <button
          onClick={handleRegister}
          disabled={regLoading || !regAlias.trim() || !regCanonical.trim() || !regEntityType.trim()}
          className={clsx(
            'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
            regLoading || !regAlias.trim() || !regCanonical.trim() || !regEntityType.trim()
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
          )}
        >
          {regLoading ? 'registering…' : 'Register'}
        </button>
        {regResult && (
          <p className={clsx(
            'text-[10px] font-mono',
            regResult.ok ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
          )}>
            {regResult.ok ? '✓ Alias registered' : `✗ ${regResult.error}`}
          </p>
        )}
      </div>
    </section>
  );
}
