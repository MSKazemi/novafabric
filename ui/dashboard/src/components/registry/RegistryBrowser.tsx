import { useMemo, useState, useEffect } from 'react';
import EmptyState from '../ui/EmptyState';
import { clsx } from 'clsx';
import { registry as fixtureRegistry, type AssetRecord, type EvalResult } from '../../lib/fixtures';

interface RegistryBrowserProps {
  /** Asset records to render. Falls back to the showcase fixture. */
  assets?: AssetRecord[];
  /** Eval results keyed by `${name}@${version}` matching `EvalResult.asset`. Falls back to fixture. */
  evalResults?: EvalResult[];
  /** Empty state message when assets is empty. */
  emptyMessage?: string;
}

const TYPE_ICON: Record<AssetRecord['asset_type'], string> = {
  model: 'M',
  prompt: 'P',
  tool: 'T',
  dataset: 'D',
  agent: 'A',
  evaluation: 'E',
  deployment: 'X',
};

const TYPE_LABEL: Record<AssetRecord['asset_type'], string> = {
  model: 'model',
  prompt: 'prompt',
  tool: 'tool',
  dataset: 'dataset',
  agent: 'agent',
  evaluation: 'evaluation',
  deployment: 'deployment',
};

export default function RegistryBrowser({
  assets: assetsInput,
  evalResults: evalResultsInput,
  emptyMessage = 'No assets registered yet. Try `nova register <spec.yaml>`.',
}: RegistryBrowserProps = {}) {
  const assetsList = assetsInput ?? fixtureRegistry.assets;
  const evalResultsList = evalResultsInput ?? fixtureRegistry.eval_results;

  const grouped = useMemo(() => {
    const map = new Map<string, AssetRecord[]>();
    for (const a of assetsList) {
      const k = a.name;
      if (!map.has(k)) map.set(k, []);
      map.get(k)!.push(a);
    }
    return map;
  }, [assetsList]);

  const initial = useMemo(
    () => assetsList.find((a) => a.name === 'code-review-prompt' && a.version === '0.2.0') ?? assetsList[0] ?? null,
    [assetsList],
  );
  const [selected, setSelected] = useState<AssetRecord | null>(initial);

  // Keep selection valid and fresh when input data changes
  useEffect(() => {
    if (!selected) { setSelected(initial); return; }
    const current = assetsList.find((a) => a.name === selected.name && a.version === selected.version);
    if (!current) { setSelected(initial); return; }
    if (current !== selected) setSelected(current);
  }, [assetsList, initial, selected]);

  if (assetsList.length === 0 || !selected) {
    return <EmptyState message={emptyMessage ?? 'No assets registered yet.'} />;
  }

  const evalResults = evalResultsList.filter((r) => r.asset === `${selected.name}@${selected.version}`);
  const versions = grouped.get(selected.name) ?? [selected];

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[280px_1fr] gap-4 min-h-[640px]">
      {/* Asset list */}
      <aside className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-3 overflow-y-auto">
        <p className="px-2 py-1.5 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] font-mono">
          {assetsList.length} assets · {grouped.size} unique names
        </p>
        <ul className="mt-2 space-y-0.5">
          {Array.from(grouped.entries()).map(([name, vs]) => (
            <li key={name}>
              <div className="px-2 py-1 text-xs text-[var(--color-text-faint)] uppercase tracking-wider font-mono">
                {TYPE_LABEL[vs[0].asset_type]}
              </div>
              {vs.map((a) => (
                <button
                  key={a.version}
                  type="button"
                  onClick={() => setSelected(a)}
                  className={clsx(
                    'w-full flex items-center gap-2 px-2 py-1.5 rounded text-sm transition-colors text-left',
                    selected.name === a.name && selected.version === a.version
                      ? 'bg-[var(--color-bg-sunken)] text-[var(--color-text)]'
                      : 'text-[var(--color-text-muted)] hover:bg-[var(--color-bg-sunken)] hover:text-[var(--color-text)]',
                  )}
                >
                  <span className={clsx(
                    'shrink-0 w-5 h-5 rounded flex items-center justify-center text-[10px] font-mono font-semibold',
                    'border border-[var(--color-border)] bg-[var(--color-bg-sunken)]',
                  )} aria-hidden="true">
                    {TYPE_ICON[a.asset_type]}
                  </span>
                  <span className="flex-1 truncate font-mono">
                    {a.name}<span className="text-[var(--color-text-faint)]">@{a.version}</span>
                  </span>
                  <StatusDot status={a.status} />
                </button>
              ))}
            </li>
          ))}
        </ul>
      </aside>

      {/* Detail */}
      <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] overflow-hidden flex flex-col">
        <header className="px-5 py-4 border-b border-[var(--color-border)] bg-[var(--color-bg-raised)]">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-xs uppercase tracking-wider text-[var(--color-text-faint)]">{TYPE_LABEL[selected.asset_type]}</p>
              <h3 className="mt-0.5 text-lg font-medium font-mono break-words">
                {selected.name}<span className="text-[var(--color-text-muted)]">@{selected.version}</span>
              </h3>
            </div>
            <div className="flex items-center gap-2">
              <StatusPill status={selected.status} />
              <button
                type="button"
                disabled
                title="Promotion runs on the CLI: nova promote <asset> --suite <suite>"
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium border border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed"
              >
                Promote
                <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true">
                  <path d="M1 5.5H8M8 5.5L4.5 2M8 5.5L4.5 9" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            </div>
          </div>
          <p className="mt-2 text-sm text-[var(--color-text-muted)]">{selected.description}</p>

          {/* Version pills */}
          {versions.length > 1 && (
            <div className="mt-3 flex items-center gap-2">
              <span className="text-xs text-[var(--color-text-faint)]">Versions:</span>
              {versions.map((v) => (
                <button
                  key={v.version}
                  type="button"
                  onClick={() => setSelected(v)}
                  className={clsx(
                    'px-2 py-0.5 text-xs rounded font-mono border transition-colors',
                    v.version === selected.version
                      ? 'border-[var(--color-accent)] text-[var(--color-text)] bg-[color-mix(in_oklab,var(--color-accent)_8%,transparent)]'
                      : 'border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
                  )}
                >
                  @{v.version}
                </button>
              ))}
            </div>
          )}
        </header>

        <div className="flex-1 grid md:grid-cols-2 gap-0 divide-y md:divide-y-0 md:divide-x divide-[var(--color-border)] overflow-hidden">
          {/* Spec */}
          <div className="p-5 overflow-auto max-h-[420px]">
            <h4 className="text-xs uppercase tracking-wider text-[var(--color-text-faint)] mb-3">Spec</h4>
            <pre className="text-xs font-mono leading-relaxed whitespace-pre-wrap text-[var(--color-text)]">
{JSON.stringify(selected.spec, null, 2)}
            </pre>
          </div>

          {/* Eval results */}
          <div className="p-5 overflow-auto max-h-[420px]">
            <h4 className="text-xs uppercase tracking-wider text-[var(--color-text-faint)] mb-3">Eval results</h4>
            {evalResults.length === 0 ? (
              (() => {
                const innerSpec = (selected.spec as Record<string, unknown>)?.spec as Record<string, unknown> | undefined;
                const hasEvalSuites = Array.isArray(innerSpec?.evals) && (innerSpec!.evals as unknown[]).length > 0;
                return hasEvalSuites ? (
                  <p className="text-sm text-[var(--color-text-muted)]">No eval results yet for this version.</p>
                ) : (
                  <p className="text-sm text-[var(--color-text-muted)]">
                    No eval suites configured.{' '}
                    <span className="text-[var(--color-text-faint)]">
                      Add an <code className="text-[10px] font-mono bg-[var(--color-bg-sunken)] px-1 rounded">evals:</code> list to the asset spec to gate promotion on eval results.
                    </span>
                  </p>
                );
              })()
            ) : (
              <ul className="space-y-2">
                {evalResults.map((r, i) => (
                  <EvalResultRow key={i} result={r} />
                ))}
              </ul>
            )}

            {selected.name === 'code-review-prompt' && selected.version === '0.2.0' && (
              <div className="mt-4 pt-4 border-t border-[var(--color-border)] rounded bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] p-3">
                <p className="text-xs text-[var(--color-status-pending)] font-medium">
                  Why is this still in development?
                </p>
                <p className="mt-1 text-xs text-[var(--color-text-muted)] leading-relaxed">
                  The <code className="text-[var(--color-text)]">edge-case-coverage</code> suite failed
                  (score 0.62, threshold 0.80). Promotion is blocked until eval passes. v0.1.0 stays in production.
                </p>
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

function StatusDot({ status }: { status: AssetRecord['status'] }) {
  return (
    <span
      className={clsx(
        'shrink-0 w-1.5 h-1.5 rounded-full',
        status === 'promoted'
          ? 'bg-[var(--color-status-success)]'
          : 'bg-[var(--color-status-pending)]',
      )}
      title={status}
    />
  );
}

function StatusPill({ status }: { status: AssetRecord['status'] }) {
  return (
    <span className={clsx(
      'inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs font-medium border',
      status === 'promoted'
        ? 'border-[color-mix(in_oklab,var(--color-status-success)_30%,transparent)] text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)]'
        : 'border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] text-[var(--color-status-pending)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)]',
    )}>
      <span className={clsx(
        'w-1.5 h-1.5 rounded-full',
        status === 'promoted' ? 'bg-[var(--color-status-success)]' : 'bg-[var(--color-status-pending)]',
      )} />
      {status}
    </span>
  );
}

function EvalResultRow({ result }: { result: EvalResult }) {
  return (
    <li className="flex items-center justify-between rounded border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-3 py-2 text-xs">
      <div className="flex items-center gap-2 min-w-0">
        {result.passed ? (
          <span className="shrink-0 w-4 h-4 rounded-full bg-[color-mix(in_oklab,var(--color-status-success)_15%,transparent)] flex items-center justify-center text-[var(--color-status-success)]">
            <svg width="9" height="9" viewBox="0 0 9 9" fill="none" aria-hidden="true">
              <path d="M1 4.5L3.5 7L8 1.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
        ) : (
          <span className="shrink-0 w-4 h-4 rounded-full bg-[color-mix(in_oklab,var(--color-status-failure)_15%,transparent)] flex items-center justify-center text-[var(--color-status-failure)]">
            <svg width="9" height="9" viewBox="0 0 9 9" fill="none" aria-hidden="true">
              <path d="M2 2L7 7M7 2L2 7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </span>
        )}
        {result.suite?.trim() ? (
          <code className="font-mono text-[var(--color-text)] truncate">{result.suite}</code>
        ) : (
          <span className="italic text-[var(--color-text-faint)] truncate">(unknown suite)</span>
        )}
      </div>
      <code className={clsx(
        'font-mono shrink-0',
        result.score === null
          ? 'text-[var(--color-text-faint)]'
          : result.passed ? 'text-[var(--color-status-success)]' : 'text-[var(--color-status-failure)]',
      )}>
        {result.score === null ? '—' : result.score.toFixed(2)}
      </code>
    </li>
  );
}
