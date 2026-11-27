/**
 * Copy-only `nova capture` command builder for the first-run onboarding panel (#265).
 *
 * Pure functions, no IO. The browser never runs the result: the user copies it into
 * their own terminal. Every flag emitted here must exist on the real CLI, so the
 * builder checks each one against the generated command registry (derived from the
 * Typer app by `ui/dashboard/scripts/gen-command-registry.py`, kept complete by
 * `tests/serve/test_command_registry_coverage.py`) and refuses to build otherwise.
 */
import { shellQuote } from '../commands/commandRegistry';
import { GENERATED_COMMANDS } from '../commands/generatedCommands';

/** Flags of `nova capture` as the generated registry knows them. */
export const CAPTURE_CLI_FLAGS: ReadonlySet<string> = new Set(
  (GENERATED_COMMANDS.find((c) => c.name === 'nova capture')?.fields ?? [])
    .filter((f) => !f.positional)
    .map((f) => f.flag ?? `--${f.key}`),
);

/** Every flag this builder can emit. A unit test asserts each one is in CAPTURE_CLI_FLAGS. */
export const BUILDER_FLAGS = ['--output-dir', '--timeout', '--environment'] as const;

/**
 * Environment variable the capture level is read from (and nowhere else):
 * `src/novafabric/cli/capture_level.py`, `nova policy capture-level get --help`.
 */
export const CAPTURE_LEVEL_ENV = 'NOVA_CAPTURE_LEVEL';

export type CaptureLevelId = 'standard' | 'minimal' | 'forensic' | 'air_gapped';

/**
 * The four values `CaptureLevel` accepts, with what each changes TODAY (experimental).
 * The notes restate `nova policy capture-level get --help`; keep them in step with it.
 */
export const CAPTURE_LEVELS: ReadonlyArray<{ id: CaptureLevelId; label: string; note: string }> = [
  {
    id: 'standard',
    label: 'standard (default)',
    note: 'No variable is set. Adapter, tool-hook and record-facade paths keep digests of tool arguments, results, state and documents.',
  },
  {
    id: 'forensic',
    label: 'forensic',
    note: 'Also keeps those payloads (tool arguments and results, state, document text) instead of digests.',
  },
  {
    id: 'minimal',
    label: 'minimal',
    note: 'Accepted, but today it behaves exactly like standard.',
  },
  {
    id: 'air_gapped',
    label: 'air_gapped',
    note: 'Today it behaves like forensic. It does not block any network call.',
  },
];

/** Starting points for the command field. The user can edit the text freely. */
export const WORKLOAD_PRESETS: ReadonlyArray<{ id: string; label: string; commandLine: string }> = [
  { id: 'python-script', label: 'Python script', commandLine: 'python agent.py' },
  { id: 'python-module', label: 'Python module', commandLine: 'python -m my_agent' },
  { id: 'uv', label: 'uv-managed project', commandLine: 'uv run python agent.py' },
  { id: 'pytest', label: 'pytest suite', commandLine: 'pytest tests/' },
  { id: 'custom', label: 'Other command', commandLine: '' },
];

/** `--environment` values offered by the builder (the CLI also accepts any custom string). */
export const ENVIRONMENT_TAGS = ['development', 'test', 'staging', 'production'] as const;

export type SplitResult = { ok: true; argv: string[] } | { ok: false; error: string };

// Characters bash or zsh would interpret outside single quotes: operators, redirects,
// substitutions, globs (`*`, `?`, `[`), brace expansion (`{`) and history expansion (`!`).
// The builder emits a literal argument list, so it refuses them rather than silently
// changing their meaning.
const UNQUOTED_SPECIAL = new Set([';', '|', '&', '<', '>', '(', ')', '$', '`', '*', '?', '[', '{', '!']);
// Inside double quotes the shell still expands `$`, backticks and (interactive bash) `!`.
const DOUBLE_QUOTED_SPECIAL = new Set(['$', '`', '!']);
// Special only at the start of a word: `~` (tilde expansion), `#` (comment), `=` (zsh
// `=cmd` path expansion).
const WORD_START_SPECIAL = new Set(['~', '#', '=']);
const ASSIGNMENT_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * Split one command line into argv the way a POSIX shell would for plain words,
 * single quotes, double quotes and backslash escapes. Anything the shell would expand
 * or interpret is refused with an explanation, never passed through:
 *
 * - unquoted `;` `|` `&` `<` `>` `(` `)` `$` `*` `?` `[` `{` `!` and backticks, and `$`, a
 *   backtick or `!` inside double quotes;
 * - an unquoted `~`, `#` or `=` at the start of a word, and an unquoted `~` right after
 *   an unquoted `=` or `:` (bash expands `x=~/y`);
 * - a leading `NAME=value` environment assignment;
 * - line breaks and other control characters.
 */
export function splitCommandLine(input: string): SplitResult {
  // Newlines too: a multi-line paste is several commands, not one.
  if (/[\u0000-\u0008\u000a-\u001f\u007f]/.test(input)) {
    return { ok: false, error: 'The command contains a line break or control character. Enter a single command on one line.' };
  }
  const argv: string[] = [];
  let cur = '';
  let inToken = false;
  let quote: "'" | '"' | null = null;
  // Whether the current word has had any quoting or escaping so far (an assignment
  // needs an unquoted NAME), and the last character appended unquoted (null after a
  // quoted or escaped one).
  let wordQuoted = false;
  let prevUnquoted: string | null = null;
  for (let i = 0; i < input.length; i++) {
    const ch = input[i];
    if (quote === "'") {
      if (ch === "'") quote = null;
      else cur += ch;
      prevUnquoted = null;
      continue;
    }
    if (quote === '"') {
      if (ch === '"') {
        quote = null;
      } else if (ch === '\\' && i + 1 < input.length && ['"', '\\', '$', '`'].includes(input[i + 1])) {
        cur += input[++i];
      } else if (DOUBLE_QUOTED_SPECIAL.has(ch)) {
        return shellSyntax(ch);
      } else {
        cur += ch;
      }
      prevUnquoted = null;
      continue;
    }
    if (ch === ' ' || ch === '\t') {
      if (inToken) argv.push(cur);
      cur = '';
      inToken = false;
      wordQuoted = false;
      prevUnquoted = null;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      inToken = true;
      wordQuoted = true;
      continue;
    }
    if (ch === '\\') {
      if (i + 1 >= input.length) return { ok: false, error: 'The command ends with a lone backslash.' };
      cur += input[++i];
      inToken = true;
      wordQuoted = true;
      prevUnquoted = null;
      continue;
    }
    if (UNQUOTED_SPECIAL.has(ch)) return shellSyntax(ch);
    if (!inToken && WORD_START_SPECIAL.has(ch)) return shellSyntax(ch);
    if (ch === '~' && (prevUnquoted === '=' || prevUnquoted === ':')) return shellSyntax(ch);
    if (ch === '=' && argv.length === 0 && !wordQuoted && ASSIGNMENT_NAME.test(cur)) {
      return {
        ok: false,
        error:
          `The command starts with an environment assignment (${cur}=…). The builder would pass it as the program name. ` +
          `Export the variable before running the copied command, or put it in front of it: ${cur}=value nova capture …`,
      };
    }
    cur += ch;
    inToken = true;
    prevUnquoted = ch;
  }
  if (quote) return { ok: false, error: `The command has an unclosed ${quote === "'" ? 'single' : 'double'} quote.` };
  if (inToken) argv.push(cur);
  if (argv.length === 0) return { ok: false, error: 'Enter the command you want to capture, for example python agent.py.' };
  return { ok: true, argv };
}

function shellSyntax(ch: string): SplitResult {
  return {
    ok: false,
    error:
      `The command uses shell syntax (${ch}). The builder writes one literal command, so it cannot keep that meaning. ` +
      'Quote the character if it is part of an argument, or put the shell logic in a script and capture the script.',
  };
}

export interface CaptureOptions {
  /** The workload command line, as the user would type it. */
  commandLine: string;
  level: CaptureLevelId;
  /** Emit `--output-dir <dir>` (the capsule directory the connected server reads). */
  outputDir?: string | null;
  /** Seconds, as typed. Empty = the CLI default. */
  timeoutSeconds?: string;
  /** One of ENVIRONMENT_TAGS, or empty for none. */
  environment?: string;
}

export type BuildResult = { ok: true; command: string; argv: string[] } | { ok: false; error: string };

/**
 * Build the exact command line to copy. Values are POSIX-shell quoted; the workload
 * argv follows `--` so none of it can be read as a `nova capture` option.
 *
 * `knownFlags` defaults to the generated registry; it is a parameter so a test can
 * prove the builder refuses a flag the CLI does not have.
 */
export function buildCaptureCommand(opts: CaptureOptions, knownFlags: ReadonlySet<string> = CAPTURE_CLI_FLAGS): BuildResult {
  const split = splitCommandLine(opts.commandLine);
  if (!split.ok) return split;

  const level = CAPTURE_LEVELS.find((l) => l.id === opts.level);
  if (!level) return { ok: false, error: `Unknown capture level: ${String(opts.level)}.` };

  const argv: string[] = [];
  const flag = (name: (typeof BUILDER_FLAGS)[number], value: string): string | null => {
    if (!knownFlags.has(name)) return `This dashboard's command registry has no ${name} for nova capture, so it is not emitted. Check nova capture --help.`;
    argv.push(name, value);
    return null;
  };

  const outputDir = opts.outputDir?.trim();
  if (outputDir) {
    if (/[\u0000-\u001f\u007f]/.test(outputDir)) return { ok: false, error: 'The capsule directory contains a control character.' };
    const err = flag('--output-dir', outputDir);
    if (err) return { ok: false, error: err };
  }

  const timeout = opts.timeoutSeconds?.trim();
  if (timeout) {
    if (!/^\d+(\.\d+)?$/.test(timeout) || Number(timeout) <= 0) {
      return { ok: false, error: 'The timeout must be a positive number of seconds.' };
    }
    const err = flag('--timeout', timeout);
    if (err) return { ok: false, error: err };
  }

  const env = opts.environment?.trim();
  if (env) {
    if (!(ENVIRONMENT_TAGS as readonly string[]).includes(env)) return { ok: false, error: `Unknown environment tag: ${env}.` };
    const err = flag('--environment', env);
    if (err) return { ok: false, error: err };
  }

  const prefix = level.id === 'standard' ? [] : [`${CAPTURE_LEVEL_ENV}=${level.id}`];
  const parts = [...prefix, 'nova', 'capture', ...argv.map(shellQuote), '--', ...split.argv.map(shellQuote)];
  return { ok: true, command: parts.join(' '), argv: ['capture', ...argv, '--', ...split.argv] };
}
