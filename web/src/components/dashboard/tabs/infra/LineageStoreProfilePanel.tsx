// Lineage Store Deployment Profile panel (nova lineage-store profile —
// v0.46.0). Extracted verbatim from InfraTab.tsx (dashboard-modernization
// split).
import { useCallback, useState } from 'react';
import { api } from '../../../../lib/api';
import CopyButton from '../../../ui/CopyButton';

export default function LineageStoreProfilePanel() {
  const [target, setTarget] = useState('kuzudb-vertical');
  const [nodeSize, setNodeSize] = useState('16g-ram-500g-nvme');
  const [rf, setRf] = useState(3);
  const [imageTag, setImageTag] = useState('latest');
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.lineageStoreProfile>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const generate = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.lineageStoreProfile({
        target,
        node_size: target === 'kuzudb-vertical' ? nodeSize : undefined,
        rf: target === 'janusgraph-minimal' ? rf : undefined,
        image_tag: imageTag || undefined,
      });
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [target, nodeSize, rf, imageTag]);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Lineage Store Deployment Profile</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Generate a docker-compose profile for a lineage backend — <code className="font-mono">nova lineage-store profile</code>
          </p>
        </div>
        <span className="text-2xs font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">v0.46</span>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <select
          value={target}
          onChange={e => setTarget(e.target.value)}
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
        >
          <option value="kuzudb-vertical">kuzudb-vertical</option>
          <option value="janusgraph-minimal">janusgraph-minimal</option>
        </select>
        {target === 'kuzudb-vertical' ? (
          <input
            value={nodeSize}
            onChange={(e) => setNodeSize(e.target.value)}
            placeholder="16g-ram-500g-nvme"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          />
        ) : (
          <input
            type="number"
            min={1}
            value={rf}
            onChange={(e) => setRf(Math.max(1, Number(e.target.value) || 1))}
            placeholder="3"
            className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
          />
        )}
        <input
          value={imageTag}
          onChange={(e) => setImageTag(e.target.value)}
          placeholder="latest"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono"
        />
      </div>
      <button
        onClick={generate}
        disabled={loading}
        className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        {loading ? '…' : 'Generate'}
      </button>
      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {err}
        </div>
      )}
      {result && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-mono text-[var(--color-text-faint)]">target: {result.target}</span>
            <CopyButton text={result.profile_yaml} label="yaml" />
          </div>
          <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2.5 whitespace-pre-wrap text-[var(--color-text-muted)] max-h-64 overflow-auto">
            {result.profile_yaml}
          </pre>
        </div>
      )}
    </section>
  );
}
