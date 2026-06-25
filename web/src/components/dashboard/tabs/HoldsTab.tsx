import { useState, useEffect, useCallback } from 'react';
import { clsx } from 'clsx';
import { api, type HoldRecord, type HoldRegistry } from '../../../lib/api';
import { ErrorBox, Loading } from '../helpers';
import { SuggestInput } from '../../ui/SuggestInput';
import EmptyState from '../../ui/EmptyState';

function HoldRow({
  hold,
  onRelease,
}: {
  hold: HoldRecord;
  onRelease: (hold_id: string) => void;
}) {
  const [releasing, setReleasing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleRelease = async () => {
    setReleasing(true);
    setError(null);
    try {
      await api.releaseHold(hold.hold_id);
      onRelease(hold.hold_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setReleasing(false);
    }
  };

  const duration = hold.duration_days != null ? `${hold.duration_days}d` : 'indefinite';
  const created = hold.created_at.slice(0, 19).replace('T', ' ');

  return (
    <li className="px-4 py-3 flex items-start gap-3">
      <span className="mt-1.5 w-2 h-2 rounded-full shrink-0 bg-amber-400" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2 flex-wrap">
          <code className="font-mono text-sm text-[var(--color-text)]">{hold.hold_id}</code>
          <span className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-amber-500 text-amber-500 uppercase tracking-wider">
            {duration}
          </span>
          <span className="text-[10px] text-[var(--color-text-faint)] font-mono">{created}</span>
        </div>
        <p className="text-xs text-[var(--color-text-muted)] mt-0.5">{hold.reason}</p>
        {error && (
          <p className="text-xs text-[var(--color-status-failure)] mt-1 font-mono">{error}</p>
        )}
      </div>
      <button
        onClick={handleRelease}
        disabled={releasing}
        className={clsx(
          'shrink-0 text-[10px] font-mono px-2 py-1 rounded border',
          releasing
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-wait'
            : 'border-[var(--color-status-failure)] text-[var(--color-status-failure)] hover:bg-[var(--color-status-failure)] hover:text-white',
        )}
      >
        {releasing ? 'releasing…' : 'release'}
      </button>
    </li>
  );
}

function PlaceHoldForm({ onCreated, registryNames }: { onCreated: () => void; registryNames: string[] }) {
  const [registry, setRegistry] = useState('');
  const [reason, setReason] = useState('');
  const [days, setDays] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!registry.trim() || !reason.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const duration = days.trim() ? parseInt(days, 10) : null;
      await api.createHold(registry.trim(), reason.trim(), duration);
      setRegistry('');
      setReason('');
      setDays('');
      onCreated();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3"
    >
      <p className="text-xs font-semibold text-[var(--color-text-muted)] uppercase tracking-wider">
        Place hold
      </p>
      <div className="grid grid-cols-[1fr_1fr_auto_auto] gap-2 items-end">
        <div className="space-y-1">
          <label className="text-[10px] text-[var(--color-text-faint)] font-mono uppercase tracking-wider">
            Registry
          </label>
          <SuggestInput
            value={registry}
            onChange={setRegistry}
            suggestions={registryNames}
            placeholder="my-registry"
            required
            className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] text-[var(--color-text-faint)] font-mono uppercase tracking-wider">
            Reason
          </label>
          <input
            type="text"
            value={reason}
            onChange={e => setReason(e.target.value)}
            placeholder="SEC exam 2026"
            required
            className="w-full text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] text-[var(--color-text-faint)] font-mono uppercase tracking-wider">
            Days (optional)
          </label>
          <input
            type="number"
            min={1}
            value={days}
            onChange={e => setDays(e.target.value)}
            placeholder="indefinite"
            className="w-24 text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
          />
        </div>
        <button
          type="submit"
          disabled={submitting || !registry.trim() || !reason.trim()}
          className={clsx(
            'text-xs font-mono px-3 py-1.5 rounded border',
            submitting || !registry.trim() || !reason.trim()
              ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
              : 'border-amber-500 text-amber-500 hover:bg-amber-500 hover:text-white',
          )}
        >
          {submitting ? 'placing…' : 'place hold'}
        </button>
      </div>
      {error && (
        <p className="text-xs text-[var(--color-status-failure)] font-mono">{error}</p>
      )}
    </form>
  );
}

export default function HoldsTab({
  refreshTick,
  onCountChange,
}: {
  refreshTick: number;
  onCountChange?: (n: number) => void;
}) {
  const [data, setData] = useState<{ total_active: number; registries: HoldRegistry[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [assetRegistryNames, setAssetRegistryNames] = useState<string[]>([]);

  const refresh = useCallback(() => {
    setError(null);
    api.listHolds()
      .then(r => {
        setData(r);
        onCountChange?.(r.total_active);
      })
      .catch(e => setError((e as Error).message));
  }, [onCountChange]);

  useEffect(() => { refresh(); }, [refresh, refreshTick]);

  useEffect(() => {
    // Collect unique registry-like prefixes from registered asset names for suggestions.
    api.listAssets({ limit: 500 })
      .then(r => {
        const names = new Set<string>();
        r.assets.forEach(a => {
          const parts = a.name.split('/');
          if (parts.length > 1) names.add(parts[0]);
        });
        setAssetRegistryNames([...names].sort());
      })
      .catch(() => {});
  }, []);

  const handleRelease = useCallback((_hold_id: string) => { refresh(); }, [refresh]);

  if (error && !data) return <ErrorBox message={error} onRetry={refresh} />;
  if (!data) return <Loading />;

  const hasHolds = data.total_active > 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between text-xs">
        <p className="text-[var(--color-text-muted)]">
          {hasHolds
            ? `${data.total_active} active legal hold${data.total_active !== 1 ? 's' : ''} across ${data.registries.filter(r => r.holds.length > 0).length} registr${data.registries.filter(r => r.holds.length > 0).length !== 1 ? 'ies' : 'y'}`
            : 'No active legal holds'}
          <span className="font-mono ml-2 text-[10px] text-[var(--color-text-faint)]">
            · ADR-0031
          </span>
        </p>
        <button onClick={refresh} className="text-[var(--color-text-muted)] hover:text-[var(--color-text)]">
          refresh
        </button>
      </div>

      <PlaceHoldForm
        onCreated={refresh}
        registryNames={[
          ...new Set([...data.registries.map(r => r.name), ...assetRegistryNames]),
        ].sort()}
      />

      {!hasHolds ? (
        <EmptyState
          message="No active holds."
          hint={<>Use the form above or <code className="font-mono text-xs">nova hold create &lt;registry&gt; --reason …</code> in the CLI.</>}
        />
      ) : (
        <div className="space-y-3">
          {data.registries.filter(r => r.holds.length > 0).map(reg => (
            <div key={reg.name} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] overflow-hidden">
              <div className="px-4 py-2 border-b border-[var(--color-border)] bg-[var(--color-bg-sunken)]">
                <span className="text-xs font-mono text-[var(--color-text-muted)] uppercase tracking-wider">
                  registry: {reg.name}
                </span>
                <span className="ml-2 text-[10px] text-[var(--color-text-faint)]">
                  {reg.holds.length} hold{reg.holds.length !== 1 ? 's' : ''}
                </span>
              </div>
              <ul className="divide-y divide-[var(--color-border)]">
                {reg.holds.map(hold => (
                  <HoldRow key={hold.hold_id} hold={hold} onRelease={handleRelease} />
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
