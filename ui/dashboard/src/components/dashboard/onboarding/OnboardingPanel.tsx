import { useEffect, useId, useMemo, useState } from 'react';
import { api } from '../../../lib/api';
import type { Tab } from '../Sidebar';
import {
  CAPTURE_LEVELS,
  CAPTURE_LEVEL_ENV,
  ENVIRONMENT_TAGS,
  WORKLOAD_PRESETS,
  buildCaptureCommand,
  type CaptureLevelId,
} from './captureCommand';

interface OnboardingPanelProps {
  /** Called with the new run count when "Check for runs" finds at least one. */
  onRunsFound: (count: number) => void;
  onNavigate: (tab: Tab) => void;
}

const FOCUS_RING =
  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]';
const CONTROL =
  `w-full rounded border border-[var(--color-border-strong)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm text-[var(--color-text)] ${FOCUS_RING}`;
const BUTTON =
  `rounded border border-[var(--color-border-strong)] px-3 py-1.5 text-xs font-medium text-[var(--color-text)] hover:bg-[var(--color-bg-sunken)] disabled:cursor-not-allowed disabled:opacity-50 ${FOCUS_RING}`;

/**
 * First-run panel shown on Home while the connected server has no runs (#265).
 *
 * Read-only and copy-only: it builds a `nova capture` command for the user to paste
 * into their own terminal, and reads `/api/runs` and `/api/stats` (GET) to find the
 * capsule directory and to re-check for runs. It never executes anything, writes
 * nothing to the server, and shows no sample or synthetic data.
 */
export default function OnboardingPanel({ onRunsFound, onNavigate }: OnboardingPanelProps) {
  const ids = {
    heading: useId(),
    workload: useId(),
    command: useId(),
    commandHint: useId(),
    commandError: useId(),
    level: useId(),
    outputDir: useId(),
    timeout: useId(),
    environment: useId(),
  };

  const [presetId, setPresetId] = useState(WORKLOAD_PRESETS[0].id);
  const [commandLine, setCommandLine] = useState(WORKLOAD_PRESETS[0].commandLine);
  const [level, setLevel] = useState<CaptureLevelId>('standard');
  const [capsuleDir, setCapsuleDir] = useState<string | null>(null);
  const [useServerDir, setUseServerDir] = useState(true);
  const [timeoutSeconds, setTimeoutSeconds] = useState('');
  const [environment, setEnvironment] = useState('');
  const [copyMessage, setCopyMessage] = useState('');
  const [checkMessage, setCheckMessage] = useState('');
  const [checking, setChecking] = useState(false);

  // Where this server looks for capsules. Unknown (request failed) = say nothing about it.
  useEffect(() => {
    let live = true;
    api.listRuns({ limit: 1 })
      .then((r) => { if (live && typeof r.capsule_dir === 'string' && r.capsule_dir) setCapsuleDir(r.capsule_dir); })
      .catch(() => { /* leave unknown */ });
    return () => { live = false; };
  }, []);

  const result = useMemo(
    () => buildCaptureCommand({
      commandLine,
      level,
      outputDir: useServerDir ? capsuleDir : null,
      timeoutSeconds,
      environment,
    }),
    [commandLine, level, useServerDir, capsuleDir, timeoutSeconds, environment],
  );

  // A copy confirmation belongs to the command that was copied.
  useEffect(() => { setCopyMessage(''); }, [result]);

  const copy = async () => {
    if (!result.ok) return;
    try {
      await navigator.clipboard.writeText(result.command);
      setCopyMessage('Command copied. Paste it into a terminal in your project directory.');
    } catch {
      setCopyMessage('Clipboard unavailable. Select the command text and copy it manually.');
    }
  };

  const checkForRuns = async () => {
    setChecking(true);
    setCheckMessage('Checking…');
    try {
      const stats = await api.getStats();
      if (stats.run_count === null) {
        setCheckMessage('The server could not count runs right now (its runs index is incomplete). Open the Runs tab to look directly.');
      } else if (stats.run_count > 0) {
        setCheckMessage(`${stats.run_count} run${stats.run_count === 1 ? '' : 's'} found.`);
        onRunsFound(stats.run_count);
      } else {
        setCheckMessage(capsuleDir ? `No runs yet in ${capsuleDir}.` : 'No runs yet.');
      }
    } catch (e) {
      setCheckMessage(`Could not check: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setChecking(false);
    }
  };

  const levelNote = CAPTURE_LEVELS.find((l) => l.id === level)?.note ?? '';

  return (
    <section
      aria-labelledby={ids.heading}
      data-testid="onboarding-panel"
      className="mb-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 sm:p-5 space-y-4"
    >
      <div>
        <h2 id={ids.heading} className="text-base font-semibold text-[var(--color-text)]">
          Capture your first run
        </h2>
        <p className="mt-1 text-sm leading-relaxed text-[var(--color-text-muted)]">
          This server has no runs yet. A run appears here after <code className="font-mono text-[var(--color-text)]">nova capture</code> writes
          its Run Capsule. The dashboard reads capsules; it never starts a capture itself.
        </p>
      </div>

      <ol className="space-y-4">
        {/* Step 1 — build */}
        <li className="space-y-3">
          <h3 className="text-sm font-medium text-[var(--color-text)]">1. Describe what to capture</h3>

          <div className="grid gap-3 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)]">
            <div>
              <label htmlFor={ids.workload} className="block text-xs font-medium text-[var(--color-text-muted)] mb-1">
                Workload
              </label>
              <select
                id={ids.workload}
                value={presetId}
                onChange={(e) => {
                  const preset = WORKLOAD_PRESETS.find((p) => p.id === e.target.value);
                  setPresetId(e.target.value);
                  if (preset) setCommandLine(preset.commandLine);
                }}
                className={CONTROL}
              >
                {WORKLOAD_PRESETS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select>
            </div>
            <div>
              <label htmlFor={ids.command} className="block text-xs font-medium text-[var(--color-text-muted)] mb-1">
                Command to capture
              </label>
              <input
                id={ids.command}
                type="text"
                value={commandLine}
                onChange={(e) => setCommandLine(e.target.value)}
                spellCheck={false}
                autoComplete="off"
                autoCapitalize="off"
                aria-invalid={!result.ok}
                aria-describedby={`${ids.commandHint}${result.ok ? '' : ` ${ids.commandError}`}`}
                placeholder="python agent.py"
                className={`${CONTROL} font-mono`}
              />
              <p id={ids.commandHint} className="mt-1 text-xs text-[var(--color-text-faint)]">
                Exactly what you would type to run it from your project directory. Framework adapters may also need a code change; see the Capture tab.
              </p>
            </div>
          </div>

          <fieldset aria-describedby={ids.level}>
            <legend className="text-xs font-medium text-[var(--color-text-muted)] mb-1">
              Capture level <span className="font-normal text-[var(--color-text-faint)]">(experimental)</span>
            </legend>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {CAPTURE_LEVELS.map((l) => (
                <label key={l.id} className="inline-flex items-center gap-1.5 text-sm text-[var(--color-text)]">
                  <input
                    type="radio"
                    name={`${ids.level}-level`}
                    value={l.id}
                    checked={level === l.id}
                    onChange={() => setLevel(l.id)}
                    className={FOCUS_RING}
                  />
                  <span className="font-mono text-xs">{l.label}</span>
                </label>
              ))}
            </div>
            <p id={ids.level} className="mt-1 text-xs text-[var(--color-text-faint)]">
              {levelNote} Set through the <code className="font-mono">{CAPTURE_LEVEL_ENV}</code> variable; see <code className="font-mono">nova policy capture-level get --help</code>.
            </p>
          </fieldset>

          {capsuleDir && (
            <div className="flex items-start gap-2">
              <input
                id={ids.outputDir}
                type="checkbox"
                checked={useServerDir}
                onChange={(e) => setUseServerDir(e.target.checked)}
                className={`mt-0.5 ${FOCUS_RING}`}
              />
              <label htmlFor={ids.outputDir} className="text-sm text-[var(--color-text)]">
                Write the capsule where this server reads (<code className="font-mono text-xs break-all">--output-dir {capsuleDir}</code>)
              </label>
            </div>
          )}

          <details className="rounded border border-[var(--color-border)] px-3 py-2">
            <summary className={`cursor-pointer text-xs font-medium text-[var(--color-text-muted)] ${FOCUS_RING}`}>
              More options
            </summary>
            <div className="mt-2 grid gap-3 sm:grid-cols-2">
              <div>
                <label htmlFor={ids.timeout} className="block text-xs font-medium text-[var(--color-text-muted)] mb-1">
                  Timeout in seconds (<code className="font-mono">--timeout</code>, optional)
                </label>
                <input
                  id={ids.timeout}
                  type="text"
                  inputMode="decimal"
                  value={timeoutSeconds}
                  onChange={(e) => setTimeoutSeconds(e.target.value)}
                  placeholder="CLI default"
                  className={CONTROL}
                />
              </div>
              <div>
                <label htmlFor={ids.environment} className="block text-xs font-medium text-[var(--color-text-muted)] mb-1">
                  Environment tag (<code className="font-mono">--environment</code>, optional)
                </label>
                <select
                  id={ids.environment}
                  value={environment}
                  onChange={(e) => setEnvironment(e.target.value)}
                  className={CONTROL}
                >
                  <option value="">none</option>
                  {ENVIRONMENT_TAGS.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
            </div>
          </details>
        </li>

        {/* Step 2 — copy */}
        <li className="space-y-2">
          <h3 className="text-sm font-medium text-[var(--color-text)]">2. Copy the command and run it in your terminal</h3>
          {result.ok ? (
            <pre
              data-testid="onboarding-command"
              className="select-all whitespace-pre-wrap break-all rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 font-mono text-xs text-[var(--color-text)]"
            >
              {result.command}
            </pre>
          ) : (
            <p id={ids.commandError} className="rounded border border-[var(--color-status-failure)] px-3 py-2 text-xs text-[var(--color-status-failure)]">
              {result.error}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" onClick={() => void copy()} disabled={!result.ok} className={BUTTON}>
              Copy capture command
            </button>
            <p role="status" aria-live="polite" className="text-xs text-[var(--color-text-muted)]">{copyMessage}</p>
          </div>
          <p className="text-xs leading-relaxed text-[var(--color-text-muted)]">
            Copy only: the dashboard does not run this command and sends nothing to the server. The syntax is for bash or zsh.
            At every capture level, model-call capture stores full prompts and responses in the capsule, so treat the
            capsule directory as sensitive.
          </p>
        </li>

        {/* Step 3 — come back */}
        <li className="space-y-2">
          <h3 className="text-sm font-medium text-[var(--color-text)]">3. Come back and check for the run</h3>
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" onClick={() => void checkForRuns()} disabled={checking} className={BUTTON}>
              Check for runs
            </button>
            <p role="status" aria-live="polite" className="text-xs text-[var(--color-text-muted)]">{checkMessage}</p>
          </div>
          {capsuleDir && (
            <p className="text-xs leading-relaxed text-[var(--color-text-faint)]">
              This server reads capsules from <code className="font-mono break-all">{capsuleDir}</code>. If yours are elsewhere,
              restart it with <code className="font-mono">nova serve --experimental --capsule-dir &lt;dir&gt;</code>.
            </p>
          )}
        </li>
      </ol>

      <div className="border-t border-[var(--color-border)] pt-3">
        <h3 className="text-sm font-medium text-[var(--color-text)]">After your first run</h3>
        <ul className="mt-2 space-y-2 text-xs leading-relaxed text-[var(--color-text-muted)]">
          <li>
            <strong className="text-[var(--color-text)]">Inspect</strong> it in the{' '}
            <button type="button" onClick={() => onNavigate('runs')} className={`text-[var(--color-accent)] underline ${FOCUS_RING}`}>Runs tab</button>,
            or read-only from the terminal: <code className="font-mono">nova replay --mode forensic &lt;run-id&gt;</code>
          </li>
          <li>
            <strong className="text-[var(--color-text)]">Compare</strong> two runs in the{' '}
            <button type="button" onClick={() => onNavigate('diff')} className={`text-[var(--color-accent)] underline ${FOCUS_RING}`}>Diff tab</button>,
            or <code className="font-mono">nova diff &lt;run-a&gt; &lt;run-b&gt;</code>
          </li>
          <li>
            <strong className="text-[var(--color-text)]">Verify</strong> a sealed capsule offline with{' '}
            <code className="font-mono">nova verify &lt;capsule-dir&gt;</code>. A capsule is sealed only when NovaSeal is configured; an unsealed capsule has no seal to verify.
          </li>
        </ul>
      </div>
    </section>
  );
}
