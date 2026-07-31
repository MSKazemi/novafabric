import { useState, useCallback } from 'react';
import { api } from '../../../../lib/api';
import { SuggestInput } from '../../../ui/SuggestInput';
import CopyButton from '../../../ui/CopyButton';

// ---------- Examiner Export panel ----------

const EXAMINER_FORMATS = [
  { value: 'bagit', label: 'BagIt (RFC 8493)', desc: 'Digital preservation archive for DSpace/Archivematica' },
  { value: 'pccp', label: 'PCCP (FDA 21 CFR)', desc: 'Predetermined Change Control Plan for SaMD' },
  { value: 'iso42001', label: 'ISO/IEC 42001', desc: 'AI Management System evidence package' },
] as const;

type ExaminerFormat = typeof EXAMINER_FORMATS[number]['value'];

export default function ExaminerPanel({ runIds }: { runIds: string[] }) {
  const [runId, setRunId] = useState('');
  const [format, setFormat] = useState<ExaminerFormat>('bagit');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; format: string; size_bytes: number; note: string } | null>(null);

  const run = useCallback(async () => {
    const id = runId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.exportExaminer(id, format);
      setResult(res);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [runId, format]);

  const cliCmd = `nova export-examiner ${format} ${runId || '<capsule_dir>'} --output ./out/${runId || 'capsule'}-${format}.zip`;
  const selectedFmt = EXAMINER_FORMATS.find(f => f.value === format);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Examiner Export</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            ADR-0062 · BagIt · PCCP · ISO 42001 evidence packages
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">ADR-0062</span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <SuggestInput
          value={runId}
          onChange={v => setRunId(v)}
          suggestions={runIds}
          onEnter={run}
          placeholder="capsule run_id"
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        />
        <select
          value={format}
          onChange={(e) => setFormat(e.target.value as ExaminerFormat)}
          className="text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2.5 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none"
        >
          {EXAMINER_FORMATS.map((f) => (
            <option key={f.value} value={f.value}>{f.label}</option>
          ))}
        </select>
      </div>

      {selectedFmt && (
        <p className="text-[10px] text-[var(--color-text-faint)]">{selectedFmt.desc}</p>
      )}

      <div className="flex gap-2 items-center">
        <div className="flex-1 font-mono text-[10px] text-[var(--color-text-faint)] px-2 py-1 bg-[var(--color-bg-sunken)] rounded border border-[var(--color-border)] truncate">
          $ {cliCmd}
        </div>
        <CopyButton text={cliCmd} label="CLI" />
        <button
          onClick={run}
          disabled={loading || !runId.trim()}
          className="px-3 py-1.5 text-xs rounded border border-[var(--color-accent)] text-[var(--color-accent)] bg-[color-mix(in_oklab,var(--color-accent)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_20%,transparent)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
        >
          {loading ? '…' : 'Export'}
        </button>
      </div>

      {error && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {error}
        </div>
      )}

      {result?.ok && (
        <div className="text-xs text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-success)_25%,transparent)] rounded px-3 py-2 space-y-1">
          <p className="font-semibold">Export generated ({(result.size_bytes / 1024).toFixed(1)} KB)</p>
          {result.note && (
            <p className="text-[10px] text-[var(--color-text-muted)]">{result.note}</p>
          )}
        </div>
      )}
    </section>
  );
}
