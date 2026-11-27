/**
 * FeatureUnavailable — one consistent "not available here" state (#283).
 *
 * Rendered in place of a feature whose backend capability the server reports
 * absent, before the user meets an error or an empty panel. It always says
 * three things in words (never colour alone): that the feature is not
 * available on this server, why, and how to enable it — plus the CLI
 * equivalent when one exists, which works wherever the extra is installed.
 */
import CopyButton from './CopyButton';

export interface FeatureUnavailableProps {
  /** Human name of the feature, e.g. "Sigstore keyless signing". */
  feature: string;
  /** Why it is not available, e.g. "the sigstore extra is not installed on this server". */
  reason: string;
  /** The exact command that enables it, e.g. `pip install 'novafabric[sigstore]'`. */
  enable: string;
  /** Shown after `enable`, e.g. "then restart nova serve". */
  enableNote?: string;
  /** The equivalent CLI command, when one exists. */
  cli?: string;
}

function CommandLine({ label, command }: { label: string; command: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-[11px] font-medium text-[var(--color-text)]">{label}</span>
      <code className="font-mono text-[11px] text-[var(--color-text)] bg-[var(--color-bg-sunken)] border border-[var(--color-border)] rounded px-1.5 py-0.5 break-all">
        {command}
      </code>
      <CopyButton text={command} label={`Copy ${label.toLowerCase()}`} />
    </div>
  );
}

export default function FeatureUnavailable({
  feature,
  reason,
  enable,
  enableNote = 'then restart nova serve',
  cli,
}: FeatureUnavailableProps) {
  return (
    <div
      role="note"
      aria-label={`${feature}: not available here`}
      data-testid="feature-unavailable"
      className="rounded border border-[color-mix(in_oklab,var(--color-status-pending)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] px-3 py-2.5 space-y-2"
    >
      <p className="text-xs text-[var(--color-text)]">
        <span className="font-semibold uppercase tracking-wider text-[10px] mr-1.5">Not available here:</span>
        <span className="font-medium">{feature}</span>
      </p>
      <p className="text-[11px] text-[var(--color-text-muted)]">
        <span className="font-medium text-[var(--color-text)]">Why: </span>
        {reason}
      </p>
      <CommandLine label="How to enable" command={enable} />
      {enableNote && <p className="text-[10px] text-[var(--color-text-faint)]">{enableNote}</p>}
      {cli && <CommandLine label="CLI equivalent" command={cli} />}
    </div>
  );
}
