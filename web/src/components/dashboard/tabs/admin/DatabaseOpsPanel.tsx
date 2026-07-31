// Database operations — Alembic upgrade, migrate-to-postgres references, and
// capsule format migration (v0.27.0). Extracted verbatim from AdminTab.tsx
// (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { CliRefRow, Panel, SectionHeading } from './helpers';

function DbUpgradeSection() {
  const inputClass =
    'rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 text-xs font-mono text-[var(--color-text)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]';

  const [revision, setRevision] = useState('head');
  const [result, setResult] = useState<{ ok: boolean; revision: string; output?: string; note: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleUpgrade = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.dbUpgrade(revision.trim() || 'head');
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [revision]);

  return (
    <div className="space-y-2">
      <p className="text-xs font-medium text-[var(--color-text)]">Alembic schema upgrade</p>
      <p className="text-[10px] text-[var(--color-text-faint)]">
        Apply any pending Alembic migrations (SQLite dev path or Postgres server path).
      </p>
      <div className="flex items-center gap-2">
        <input
          value={revision}
          onChange={(e) => setRevision(e.target.value)}
          placeholder="head"
          className={clsx(inputClass, 'w-32')}
        />
        <button
          onClick={handleUpgrade}
          disabled={loading}
          className="text-xs font-mono px-3 py-1.5 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] disabled:opacity-50 transition-colors"
        >
          {loading ? 'Running…' : 'Run upgrade'}
        </button>
      </div>
      {result && (
        <div className={clsx(
          'rounded border px-3 py-2 text-[10px] font-mono',
          result.ok
            ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] text-[var(--color-status-success)]'
            : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] text-[var(--color-status-failure)]',
        )}>
          <p>{result.note}</p>
          {result.output && <p className="mt-1 text-[var(--color-text-muted)]">{result.output}</p>}
        </div>
      )}
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
    </div>
  );
}

function CapsuleMigrateSection() {
  const inputClass =
    'w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 text-xs font-mono text-[var(--color-text)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]';
  const labelClass = 'block text-[10px] font-mono text-[var(--color-text-faint)] mb-1';

  const [source, setSource] = useState('');
  const [output, setOutput] = useState('');
  const [result, setResult] = useState<{
    ok: boolean;
    source: string;
    output: string;
    files_migrated?: number;
    files_updated?: string[];
    lineage_edge_id?: string;
    note: string;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleMigrate = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    if (!source.trim() || !output.trim()) return;
    setLoading(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.capsuleMigrate(source.trim(), output.trim());
      setResult(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [source, output]);

  return (
    <div className="space-y-3">
      <p className="text-xs font-medium text-[var(--color-text)]">Capsule format migration</p>
      <p className="text-[10px] text-[var(--color-text-faint)]">
        Convert a v0.1.x capsule directory to v1.0.0 format (ADR-0034 §6).
        The source directory is never modified — output is written to a new path.
      </p>
      <form onSubmit={handleMigrate} className="space-y-3">
        <div>
          <label className={labelClass}>Source path (v0.1.x capsule directory)</label>
          <input
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="/path/to/capsule-v01x"
            className={inputClass}
          />
        </div>
        <div>
          <label className={labelClass}>Output path (must not already exist)</label>
          <input
            value={output}
            onChange={(e) => setOutput(e.target.value)}
            placeholder="/path/to/capsule-v100"
            className={inputClass}
          />
        </div>
        <button
          type="submit"
          disabled={loading || !source.trim() || !output.trim()}
          className="text-xs font-mono px-3 py-1.5 rounded border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] disabled:opacity-50 transition-colors"
        >
          {loading ? 'Migrating…' : 'Migrate capsule'}
        </button>
      </form>
      {result && (
        <div className={clsx(
          'rounded border px-3 py-2 text-[10px] font-mono space-y-1',
          result.ok
            ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] text-[var(--color-status-success)]'
            : 'border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] text-[var(--color-status-failure)]',
        )}>
          <p>{result.note}</p>
          {result.ok && result.files_migrated !== undefined && (
            <p className="text-[var(--color-text-muted)]">Files updated: {result.files_migrated}</p>
          )}
          {result.ok && result.lineage_edge_id && (
            <p className="text-[var(--color-text-muted)]">Lineage edge: {result.lineage_edge_id}</p>
          )}
        </div>
      )}
      {err && <p className="text-xs text-[var(--color-status-failure)] font-mono">{err}</p>}
      <CliRefRow cmd="nova migrate <source> --output <output>" label="CLI reference" />
    </div>
  );
}

export default function DatabaseOpsPanel() {
  return (
    <Panel>
      <SectionHeading>Database operations</SectionHeading>
      <div className="space-y-6">
        {/* Interactive: DB upgrade */}
        <DbUpgradeSection />

        {/* CLI-only: migrate-to-postgres */}
        <div className="space-y-2">
          <p className="text-xs font-medium text-[var(--color-text)]">Migrate SQLite → Postgres</p>
          <p className="text-[10px] text-[var(--color-text-faint)]">
            One-time idempotent migration. Requires <code className="font-mono text-[10px]">NOVA_POSTGRES_URL</code>.
          </p>
          <CliRefRow cmd="nova db migrate-to-postgres --postgres-url postgresql://user:pass@host:5432/nova" label="CLI reference" />
        </div>

        {/* CLI-only: rebuild metadata DB */}
        <div className="space-y-2">
          <p className="text-xs font-medium text-[var(--color-text)]">Rebuild metadata DB</p>
          <p className="text-[10px] text-[var(--color-text-faint)]">
            Disaster-recovery path — rebuild the metadata DB from the manifest chain log.
            Completes in minutes, not hours (AC-1, BQ-013).
          </p>
          <CliRefRow cmd="nova rebuild-metadata-db" label="CLI reference" />
        </div>

        {/* Interactive: Capsule format migration */}
        <CapsuleMigrateSection />
      </div>
    </Panel>
  );
}
