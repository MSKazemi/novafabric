import { useEffect, useState } from 'react';

const FRAMES = [
  { delayMs: 400, line: '$ nova capture python agent.py', kind: 'cmd' as const },
  { delayMs: 600, line: 'Capturing run 01KR5SQZPDGTKE3MDP3ZRX8WP1', kind: 'log' as const },
  { delayMs: 200, line: '  ↳ wrapping stdin/stdout, hooking openai + requests + mcp', kind: 'log' as const },
  { delayMs: 700, line: '  ↳ recorded 2 model calls, 2 tool calls, 1 mutating', kind: 'log' as const },
  { delayMs: 350, line: '  ↳ env.lock, redaction-proof.json, lineage.jsonl emitted', kind: 'log' as const },
  { delayMs: 600, line: '✓ capsule written to .novafabric/runs/01KR5SQZPDGTKE3MDP3ZRX8WP1', kind: 'ok' as const },
  { delayMs: 900, line: '$ nova replay 01KR5SQZPDGTKE3MDP3ZRX8WP1 --mode mocked', kind: 'cmd' as const },
  { delayMs: 600, line: '✓ replay matches: 2/2 model calls, 2/2 tool calls served from cache', kind: 'ok' as const },
];

export default function TerminalReplay() {
  const [frame, setFrame] = useState(0);

  useEffect(() => {
    if (frame >= FRAMES.length) {
      const restart = setTimeout(() => setFrame(0), 4000);
      return () => clearTimeout(restart);
    }
    const t = setTimeout(() => setFrame((f) => f + 1), FRAMES[frame].delayMs);
    return () => clearTimeout(t);
  }, [frame]);

  return (
    <div
      role="img"
      aria-label="Terminal demonstration of nova capture and nova replay"
      className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-sunken)] shadow-2xl shadow-black/40 overflow-hidden font-mono text-sm"
    >
      <div className="flex items-center gap-2 px-4 py-2 border-b border-[var(--color-border)] bg-[var(--color-bg-raised)]">
        <span className="w-2.5 h-2.5 rounded-full bg-[var(--color-text-faint)]/40" />
        <span className="w-2.5 h-2.5 rounded-full bg-[var(--color-text-faint)]/40" />
        <span className="w-2.5 h-2.5 rounded-full bg-[var(--color-text-faint)]/40" />
        <span className="ml-2 text-xs text-[var(--color-text-faint)]">~/projects/agent</span>
      </div>
      <div className="px-4 py-4 min-h-[240px]">
        {FRAMES.slice(0, frame).map((f, i) => (
          <div
            key={i}
            className={
              f.kind === 'cmd'
                ? 'text-[var(--color-text)]'
                : f.kind === 'ok'
                ? 'text-[var(--color-status-success)]'
                : 'text-[var(--color-text-muted)]'
            }
          >
            {f.line}
          </div>
        ))}
        {frame < FRAMES.length && (
          <span className="inline-block w-2 h-4 align-middle bg-[var(--color-accent)] animate-pulse" aria-hidden="true" />
        )}
      </div>
    </div>
  );
}
