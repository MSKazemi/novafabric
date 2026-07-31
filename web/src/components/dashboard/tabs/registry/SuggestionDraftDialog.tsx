/**
 * C-2 draft spec review modal: edit a suggestion's draft YAML and register it.
 * Extracted verbatim from the former RegistryTab monolith — behavior frozen
 * (including the by-id textarea read on submit).
 */
import type { RegistrationSuggestion } from '../../../../lib/api';

export default function SuggestionDraftDialog({
  suggestion,
  busy,
  onRegister,
  onClose,
}: {
  suggestion: RegistrationSuggestion;
  busy: boolean;
  onRegister: (yaml: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="w-full max-w-lg rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] shadow-xl p-5 space-y-4">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-sm font-semibold text-[var(--color-text)]">
              Register {suggestion.asset_type}: {suggestion.detected_name}
            </h2>
            <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
              Review and edit the draft spec, then register.
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-[var(--color-text-faint)] hover:text-[var(--color-text)] text-lg leading-none"
            aria-label="Close"
          >×</button>
        </div>
        {suggestion.warnings.length > 0 && (
          <div className="space-y-1">
            {suggestion.warnings.map((w, i) => (
              <p key={i} className="text-[11px] text-[var(--color-status-pending)]">⚠ {w}</p>
            ))}
          </div>
        )}
        <label className="block">
          <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">YAML spec</span>
          <textarea
            defaultValue={suggestion.draft_spec_yaml}
            id="suggestion-draft-yaml"
            rows={14}
            className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 font-mono text-xs focus:border-[var(--color-accent)] focus:outline-none"
          />
        </label>
        <div className="flex gap-2 justify-end">
          <button
            onClick={onClose}
            className="px-3 py-1.5 rounded border border-[var(--color-border)] text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
          >Cancel</button>
          <button
            disabled={busy}
            onClick={() => {
              const textarea = document.getElementById('suggestion-draft-yaml') as HTMLTextAreaElement;
              const yaml = textarea?.value ?? suggestion.draft_spec_yaml;
              onRegister(yaml);
            }}
            className="px-3 py-1.5 rounded border border-[var(--color-accent)] bg-[var(--color-accent)] text-[var(--color-accent-fg)] text-xs font-medium hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
          >{busy ? 'Registering…' : 'Register'}</button>
        </div>
      </div>
    </div>
  );
}
