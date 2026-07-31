import { useState, useCallback } from 'react';
import { ping, validateToken, setConnection } from '../../lib/api';

interface ConnectPanelProps {
  onConnected: (info: { version: string; base: string }) => void;
  bootError: string | null;
  setBootError: (m: string | null) => void;
}

export default function ConnectPanel({ onConnected, bootError, setBootError }: ConnectPanelProps) {
  const [token, setToken] = useState('');
  const [base, setBase] = useState('http://127.0.0.1:4444');
  const [busy, setBusy] = useState(false);

  const onSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setBootError(null);
    setBusy(true);
    try {
      await validateToken(base.trim(), token.trim());
      const h = await ping(base.trim());
      setConnection(token.trim(), base.trim());
      onConnected({ version: h.version, base: base.trim() });
    } catch (e) {
      setBootError(`Could not connect: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }, [token, base, onConnected, setBootError]);

  return (
    <div className="flex items-center justify-center h-full min-h-screen bg-[var(--color-bg)]">
      <div className="w-full max-w-md rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-8 shadow-[var(--shadow-2)]">
        <div className="mb-6">
          <span className="flex items-center gap-2">
            <svg viewBox="0 0 32 32" width="24" height="24" fill="none" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" className="shrink-0">
              <rect width="32" height="32" rx="7" fill="var(--color-bg-sunken)"/>
              <polygon points="16,3.5 27.5,10 27.5,22 16,28.5 4.5,22 4.5,10" stroke="var(--color-accent)" strokeWidth="1.5" fill="none" strokeLinejoin="round"/>
              <path d="M10 23 L10 9 L22 23 L22 9" stroke="var(--color-accent)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" fill="none"/>
            </svg>
            <span className="text-2xs font-mono uppercase tracking-widest text-[var(--color-text-faint)]">NovaFabric</span>
          </span>
          <h1 className="mt-3 text-2xl font-medium text-[var(--color-text)] tracking-tight">Connect to <code className="font-mono text-[var(--color-text-muted)]">nova serve</code></h1>
          <p className="mt-2 text-sm text-[var(--color-text-muted)] leading-relaxed">
            Run{' '}
            <code className="font-mono text-xs bg-[var(--color-bg-sunken)] px-1.5 py-0.5 rounded">nova serve --experimental</code>{' '}
            in your terminal, then paste the URL and token it prints.
          </p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4">
          <label className="block">
            <span className="text-2xs uppercase tracking-wider text-[var(--color-text-faint)]">Server URL</span>
            <input
              type="url"
              value={base}
              onChange={(e) => setBase(e.target.value)}
              placeholder="http://127.0.0.1:4444"
              className="mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 font-mono text-sm focus:border-[var(--color-accent)] focus:outline-none"
              required
            />
          </label>

          <label className="block">
            <span className="text-2xs uppercase tracking-wider text-[var(--color-text-faint)]">Session token</span>
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="paste token from terminal"
              autoComplete="off"
              className="mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 font-mono text-sm focus:border-[var(--color-accent)] focus:outline-none"
              required
            />
            <p className="mt-1 text-2xs text-[var(--color-text-faint)]">Also at <code>~/.novafabric/.serve-token</code> (mode 0600)</p>
          </label>

          {bootError && (
            <div className="rounded border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[var(--color-danger-tint)] p-3 text-xs text-[var(--color-status-failure)]">
              {bootError}
            </div>
          )}

          <button
            type="submit"
            disabled={busy || !token || !base}
            className="w-full inline-flex items-center justify-center px-4 py-2.5 rounded-md text-sm font-medium bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-60 transition-colors"
          >
            {busy ? 'Connecting…' : 'Connect →'}
          </button>
        </form>
      </div>
    </div>
  );
}
