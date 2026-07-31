// Risk classification from a capsule (ADR-0056) + result display. Extracted
// verbatim from GovernanceTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';
import { EU_TIER_CONFIG, NIST_IMPACT_CONFIG, VOCAB_OPTIONS } from './tiers';

// ── Classification result display ─────────────────────────────────────────────

interface ClassifyResult {
  eu_ai_act_tier: string;
  eu_ai_act_role: string;
  eu_ai_act_annex_iii_items: Array<{ item_id: string; description: string; citation: string }>;
  nist_rmf_impact: string;
  omb_flags: string[];
  rationale: string;
  citations: string[];
  vocabulary_version: string;
}

function ClassifyResultPanel({
  result,
  runId,
  vocabulary,
}: {
  result: ClassifyResult;
  runId: string;
  vocabulary: string;
}) {
  const euConfig = EU_TIER_CONFIG[result.eu_ai_act_tier] ?? { label: result.eu_ai_act_tier, colorClass: 'text-[var(--color-text-muted)]' };
  const nistConfig = NIST_IMPACT_CONFIG[result.nist_rmf_impact] ?? { label: result.nist_rmf_impact, colorClass: 'text-[var(--color-text-muted)]' };
  const cliCmd = `nova classify from-capsule <capsule_dir> --vocabulary ${vocabulary}`;

  return (
    <div className="space-y-3">
      {/* Primary tier badges */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className={clsx('text-xs font-mono font-bold px-3 py-1.5 rounded border', euConfig.colorClass)}>
          EU AI Act: {euConfig.label}
        </div>
        <div className={clsx('text-xs font-mono font-bold px-2 py-1 rounded', nistConfig.colorClass)}>
          NIST RMF: {nistConfig.label} impact
        </div>
        {result.omb_flags.length > 0 && (
          <div className="text-xs font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)]">
            OMB: {result.omb_flags.join(', ')}
          </div>
        )}
        {result.omb_flags.length === 0 && (
          <div className="text-xs font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
            OMB: no flags
          </div>
        )}
      </div>

      {/* Role */}
      <div className="flex items-center gap-2 text-[10px] text-[var(--color-text-faint)] font-mono">
        <span>role: {result.eu_ai_act_role}</span>
        <span className="opacity-40">·</span>
        <span className="truncate">vocabularies: {result.vocabulary_version}</span>
      </div>

      {/* Rationale */}
      <div className="space-y-1">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Rationale</p>
        <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2.5 whitespace-pre-wrap text-[var(--color-text-muted)] max-h-40 overflow-auto">
          {result.rationale}
        </pre>
      </div>

      {/* Matched Annex III items */}
      {result.eu_ai_act_annex_iii_items.length > 0 && (
        <div className="space-y-1">
          <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Matched Annex III items
          </p>
          <ul className="space-y-1">
            {result.eu_ai_act_annex_iii_items.map((item) => (
              <li
                key={item.item_id}
                className="text-[10px] font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 space-y-0.5"
              >
                <span className="text-[var(--color-text)]">[{item.item_id}] {item.description}</span>
                <span className="block text-[var(--color-text-faint)]">{item.citation}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* CLI equivalent */}
      <div className="space-y-1">
        <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">CLI equivalent</p>
        <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
          <pre className="text-[10px] font-mono text-[var(--color-text-muted)]">{cliCmd}</pre>
          <div className="absolute top-1.5 right-1.5">
            <CopyButton text={cliCmd} label="CLI" />
          </div>
        </div>
        <p className="text-[var(--text-2xs)] text-[var(--color-text-faint)]">
          Note: dashboard classification uses minimal capsule metadata. Run the CLI with a full system description file for detailed results.
        </p>
      </div>
    </div>
  );
}

// ── Risk Classification panel ─────────────────────────────────────────────────

export default function ClassifyPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [vocabulary, setVocabulary] = useState('eu-ai-act/2024.1.0');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ClassifyResult | null>(null);

  const run = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.classifyFromCapsule(id, vocabulary);
      setResult(res.result as unknown as ClassifyResult);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, vocabulary]);

  const inputClass =
    'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Risk Classification</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            ADR-0056 · EU AI Act 2024/1689 · NIST AI RMF 600-1 · OMB M-24-10
          </p>
        </div>
        <span className="text-[var(--text-2xs)] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]">
          ADR-0056
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Capsule run ID
          </label>
          <SuggestInput
            value={runId}
            onChange={setRunId}
            suggestions={runIds}
            placeholder="run_2024_..."
            className={inputClass + ' w-full'}
            onEnter={run}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Vocabulary
          </label>
          <select
            value={vocabulary}
            onChange={(e) => setVocabulary(e.target.value)}
            className={inputClass + ' w-full'}
          >
            {VOCAB_OPTIONS.map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </div>
      </div>

      <button
        onClick={run}
        disabled={loading || !runId.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !runId.trim()
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'classifying…' : 'Classify'}
      </button>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <ClassifyResultPanel result={result} runId={runId.trim()} vocabulary={vocabulary} />
      )}
    </section>
  );
}
