// Manual risk classification (nova classify run). Extracted verbatim from
// GovernanceTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';
import CopyButton from '../../../ui/CopyButton';
import { EU_TIER_CONFIG, NIST_IMPACT_CONFIG } from './tiers';

export default function ManualClassifyPanel() {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [useCaseDomain, setUseCaseDomain] = useState('');
  const [deploymentContext, setDeploymentContext] = useState('');
  const [usesBiometrics, setUsesBiometrics] = useState(false);
  const [affectsFundamentalRights, setAffectsFundamentalRights] = useState(false);
  const [isGeneralPurpose, setIsGeneralPurpose] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);

  const run = useCallback(async () => {
    if (!name.trim() || !description.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.classifyManual({
        name: name.trim(),
        description: description.trim(),
        use_case_domain: useCaseDomain.trim(),
        deployment_context: deploymentContext.trim(),
        uses_biometrics: usesBiometrics,
        affects_fundamental_rights: affectsFundamentalRights,
        is_general_purpose: isGeneralPurpose,
      });
      setResult(res.classification);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [name, description, useCaseDomain, deploymentContext, usesBiometrics, affectsFundamentalRights, isGeneralPurpose]);

  const inputClass =
    'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';
  const textareaClass =
    'w-full text-[10px] rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-2 font-mono focus:border-[var(--color-accent)] focus:outline-none resize-y min-h-[80px]';

  const euTier = typeof result?.eu_ai_act_tier === 'string' ? result.eu_ai_act_tier : null;
  const nistLevel = typeof result?.nist_rmf_level === 'string' ? result.nist_rmf_level : null;
  const ombTier = typeof result?.omb_tier === 'string' ? result.omb_tier : null;
  const rationale = typeof result?.rationale === 'string' ? result.rationale : null;
  const euConfig = euTier ? (EU_TIER_CONFIG[euTier] ?? { label: euTier, colorClass: 'text-[var(--color-text-muted)]' }) : null;
  const nistConfig = nistLevel ? (NIST_IMPACT_CONFIG[nistLevel] ?? { label: nistLevel, colorClass: 'text-[var(--color-text-muted)]' }) : null;

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Manual Risk Classification</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Classify an AI system from a manual description — <code className="font-mono">nova classify run</code>
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Name
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-ai-system"
            className={inputClass + ' w-full'}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
            Use case domain
          </label>
          <input
            type="text"
            value={useCaseDomain}
            onChange={(e) => setUseCaseDomain(e.target.value)}
            placeholder="finance, healthcare, general"
            className={inputClass + ' w-full'}
          />
        </div>
      </div>

      <div className="space-y-1">
        <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          Description
        </label>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What the system does, who uses it, and how decisions affect people."
          className={textareaClass}
        />
      </div>

      <div className="space-y-1">
        <label className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">
          Deployment context
        </label>
        <input
          type="text"
          value={deploymentContext}
          onChange={(e) => setDeploymentContext(e.target.value)}
          placeholder="credit_scoring"
          className={inputClass + ' w-full'}
        />
      </div>

      <div className="flex items-center gap-4 flex-wrap">
        <label className="flex items-center gap-1.5 text-[10px] font-mono text-[var(--color-text-muted)] cursor-pointer">
          <input
            type="checkbox"
            checked={usesBiometrics}
            onChange={(e) => setUsesBiometrics(e.target.checked)}
            className="accent-[var(--color-accent)]"
          />
          uses biometrics
        </label>
        <label className="flex items-center gap-1.5 text-[10px] font-mono text-[var(--color-text-muted)] cursor-pointer">
          <input
            type="checkbox"
            checked={affectsFundamentalRights}
            onChange={(e) => setAffectsFundamentalRights(e.target.checked)}
            className="accent-[var(--color-accent)]"
          />
          affects fundamental rights
        </label>
        <label className="flex items-center gap-1.5 text-[10px] font-mono text-[var(--color-text-muted)] cursor-pointer">
          <input
            type="checkbox"
            checked={isGeneralPurpose}
            onChange={(e) => setIsGeneralPurpose(e.target.checked)}
            className="accent-[var(--color-accent)]"
          />
          general purpose
        </label>
      </div>

      <button
        onClick={run}
        disabled={loading || !name.trim() || !description.trim()}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading || !name.trim() || !description.trim()
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
        <div className="space-y-3">
          {/* Primary tier badges */}
          <div className="flex items-center gap-3 flex-wrap">
            {euConfig && (
              <div className={clsx('text-xs font-mono font-bold px-3 py-1.5 rounded border', euConfig.colorClass)}>
                EU AI Act: {euConfig.label}
              </div>
            )}
            {nistConfig && (
              <div className={clsx('text-xs font-mono font-bold px-2 py-1 rounded', nistConfig.colorClass)}>
                NIST RMF: {nistConfig.label}
              </div>
            )}
            {ombTier && (
              <div className="text-xs font-mono px-2 py-1 rounded border border-[var(--color-border)] text-[var(--color-text-muted)]">
                OMB: {ombTier}
              </div>
            )}
          </div>

          {rationale && (
            <div className="space-y-1">
              <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]">Rationale</p>
              <pre className="text-[10px] font-mono bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] p-2.5 whitespace-pre-wrap text-[var(--color-text-muted)] max-h-40 overflow-auto">
                {rationale}
              </pre>
            </div>
          )}

          {/* Full JSON */}
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-[var(--color-text-faint)]">Full classification JSON</span>
            <CopyButton text={JSON.stringify(result, null, 2)} label="JSON" />
          </div>
        </div>
      )}
    </section>
  );
}
