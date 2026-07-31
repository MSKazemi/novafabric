// Regulatory vocabularies listing (nova classify list-vocabularies).
// Extracted verbatim from GovernanceTab.tsx (dashboard-modernization split).
import { useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../../lib/api';

interface VocabularyRow {
  framework: string;
  version: string;
  reference: string;
  path: string;
}

export default function VocabulariesPanel() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; vocabularies: VocabularyRow[] } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.governanceVocabularies();
      setResult(res);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Regulatory Vocabularies</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Available EU AI Act / NIST AI RMF vocabulary versions — <code className="font-mono">nova classify list-vocabularies</code>
          </p>
        </div>
      </div>

      <button
        onClick={load}
        disabled={loading}
        className={clsx(
          'text-xs font-mono px-4 py-1.5 rounded border transition-colors',
          loading
            ? 'border-[var(--color-border)] text-[var(--color-text-faint)] cursor-not-allowed'
            : 'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
        )}
      >
        {loading ? 'loading…' : 'Load'}
      </button>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result && result.vocabularies.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-[10px] font-mono border-collapse">
            <thead>
              <tr className="border-b border-[var(--color-border)]">
                {['Framework', 'Version', 'Reference', 'Path'].map((h) => (
                  <th key={h} className="text-left px-2 py-1 text-[var(--color-text-faint)] uppercase tracking-wider font-normal">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.vocabularies.map((v) => (
                <tr key={`${v.framework}/${v.version}`} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-bg-sunken)]">
                  <td className="px-2 py-1.5 text-[var(--color-text)]">{v.framework}</td>
                  <td className="px-2 py-1.5 text-[var(--color-text-muted)]">{v.version}</td>
                  <td className="px-2 py-1.5 text-[var(--color-text-muted)]">{v.reference}</td>
                  <td className="px-2 py-1.5 text-[var(--color-text-faint)]">{v.path}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {result && result.vocabularies.length === 0 && (
        <p className="text-xs text-[var(--color-text-faint)]">No vocabularies found.</p>
      )}
    </section>
  );
}
