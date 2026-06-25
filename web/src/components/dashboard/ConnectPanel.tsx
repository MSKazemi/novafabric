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
      <div className="w-full max-w-md rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-8 shadow-xl">
        <div className="mb-6">
          <span className="text-[10px] font-mono uppercase tracking-widest text-[var(--color-text-faint)]">NovaFabric</span>
          <h1 className="mt-2 text-2xl font-medium text-[var(--color-text)]">Connect to <code className="font-mono text-[var(--color-text-muted)]">nova serve</code></h1>
          <p className="mt-2 text-sm text-[var(--color-text-muted)] leading-relaxed">
            Run{' '}
            <code className="font-mono text-xs bg-[var(--color-bg-sunken)] px-1.5 py-0.5 rounded">nova serve --experimental</code>{' '}
            in your terminal, then paste the URL and token it prints.
          </p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4">
          <label className="block">
            <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Server URL</span>
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
            <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">Session token</span>
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="paste token from terminal"
              autoComplete="off"
              className="mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 font-mono text-sm focus:border-[var(--color-accent)] focus:outline-none"
              required
            />
            <p className="mt-1 text-[10px] text-[var(--color-text-faint)]">Also at <code>~/.novafabric/.serve-token</code> (mode 0600)</p>
          </label>

          {bootError && (
            <div className="rounded border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] p-3 text-xs text-[var(--color-status-failure)]">
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
