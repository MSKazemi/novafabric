import { useState } from 'react';

type SupportContext = 'disconnected' | 'health';

/**
 * Read-only support handoff. Commands are static; the browser never executes
 * them, reads local diagnostic files, or sends the resulting archive anywhere.
 */
export default function SupportHandoff({ context }: { context: SupportContext }) {
  const [copyMessage, setCopyMessage] = useState('');

  const copyCommand = async (command: 'nova doctor' | 'nova support-bundle') => {
    try {
      await navigator.clipboard.writeText(command);
      setCopyMessage(`${command} copied.`);
    } catch {
      setCopyMessage('Clipboard unavailable. Select the command text and copy it manually.');
    }
  };

  const commands = [
    { command: 'nova doctor' as const, description: 'Check your local installation and dependencies.' },
    { command: 'nova support-bundle' as const, description: 'Create an experimental, redacted diagnostic archive on your own computer.' },
  ];

  return (
    <section aria-label="Local troubleshooting" className="mt-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3">
      <h2 className="text-sm font-semibold text-[var(--color-text)]">Troubleshoot locally</h2>
      <p className="mt-1 text-xs leading-relaxed text-[var(--color-text-muted)]">
        {context === 'disconnected'
          ? 'The dashboard is not connected. Check that nova serve --experimental is running, then retry with your current session token.'
          : 'A health check failed or could not complete. The following commands can help investigate locally.'}
      </p>
      <ol className="mt-3 space-y-2">
        {commands.map(({ command, description }) => (
          <li key={command} className="rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2">
            <p className="text-xs text-[var(--color-text-muted)]">{description}</p>
            <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2">
              <code className="select-all break-all text-xs text-[var(--color-text)]">{command}</code>
              <button
                type="button"
                aria-label={`Copy ${command}`}
                onClick={() => void copyCommand(command)}
                className="rounded border border-[var(--color-border-strong)] px-2.5 py-1.5 text-xs font-medium text-[var(--color-text)] hover:bg-[var(--color-bg-raised)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
              >
                Copy
              </button>
            </div>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-xs leading-relaxed text-[var(--color-text-muted)]">
        Commands run only if you paste them into a terminal. The support archive stays local; nothing is uploaded automatically.
        Review its contents, especially optional log entries, before choosing to share it. Redaction cannot guarantee every log is safe to disclose.
      </p>
      <p role="status" aria-live="polite" className="mt-2 text-xs text-[var(--color-text-muted)]">{copyMessage}</p>
    </section>
  );
}
