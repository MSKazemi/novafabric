/**
 * #265 — the onboarding command builder emits only real `nova capture` flags, quotes every
 * value, and refuses shell syntax instead of silently changing what the user typed.
 */
import { describe, expect, it } from 'vitest';

import {
  BUILDER_FLAGS,
  CAPTURE_CLI_FLAGS,
  CAPTURE_LEVELS,
  WORKLOAD_PRESETS,
  buildCaptureCommand,
  splitCommandLine,
} from '@/components/dashboard/onboarding/captureCommand';

describe('flags come from the generated CLI registry', () => {
  it('knows nova capture', () => {
    expect(CAPTURE_CLI_FLAGS.size).toBeGreaterThan(0);
  });

  it('every flag the builder can emit exists on nova capture', () => {
    expect(BUILDER_FLAGS.filter((f) => !CAPTURE_CLI_FLAGS.has(f))).toEqual([]);
  });

  it('every flag in a fully loaded command is a real nova capture flag', () => {
    const r = buildCaptureCommand({
      commandLine: 'python agent.py --verbose',
      level: 'forensic',
      outputDir: '/home/me/.novafabric/capsules',
      timeoutSeconds: '300',
      environment: 'staging',
    });
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    const sep = r.argv.indexOf('--');
    const novaFlags = r.argv.slice(1, sep).filter((t) => t.startsWith('--'));
    expect(novaFlags).toEqual(['--output-dir', '--timeout', '--environment']);
    for (const f of novaFlags) expect(CAPTURE_CLI_FLAGS.has(f)).toBe(true);
  });

  it('refuses to emit a flag the registry does not have', () => {
    const r = buildCaptureCommand(
      { commandLine: 'python agent.py', level: 'standard', timeoutSeconds: '30' },
      new Set(['--output-dir']),
    );
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.error).toMatch(/--timeout/);
  });
});

describe('buildCaptureCommand', () => {
  it('builds the plain default command with the workload after --', () => {
    const r = buildCaptureCommand({ commandLine: 'python agent.py', level: 'standard' });
    expect(r).toEqual({ ok: true, command: 'nova capture -- python agent.py', argv: ['capture', '--', 'python', 'agent.py'] });
  });

  it('keeps workload flags after -- so nova capture never parses them', () => {
    const r = buildCaptureCommand({ commandLine: 'python agent.py --timeout 5', level: 'standard' });
    expect(r.ok && r.command).toBe('nova capture -- python agent.py --timeout 5');
  });

  it('sets a non-default capture level through NOVA_CAPTURE_LEVEL, never a flag', () => {
    const r = buildCaptureCommand({ commandLine: 'python agent.py', level: 'forensic' });
    expect(r.ok && r.command).toBe('NOVA_CAPTURE_LEVEL=forensic nova capture -- python agent.py');
  });

  it('offers exactly the four levels the CLI accepts', () => {
    expect(CAPTURE_LEVELS.map((l) => l.id).sort()).toEqual(['air_gapped', 'forensic', 'minimal', 'standard']);
  });

  it('does not describe air_gapped as blocking network calls', () => {
    const note = CAPTURE_LEVELS.find((l) => l.id === 'air_gapped')!.note;
    expect(note).toMatch(/does not block/i);
  });

  it('quotes a capsule directory with spaces and a single quote', () => {
    const r = buildCaptureCommand({ commandLine: 'python agent.py', level: 'standard', outputDir: "/tmp/it's here" });
    expect(r.ok && r.command).toBe("nova capture --output-dir '/tmp/it'\\''s here' -- python agent.py");
  });

  it('re-quotes quoted workload arguments so the shell sees the same argv', () => {
    const r = buildCaptureCommand({ commandLine: `python agent.py --prompt "hello world" 'a;b'`, level: 'standard' });
    expect(r.ok && r.command).toBe("nova capture -- python agent.py --prompt 'hello world' 'a;b'");
    expect(r.ok && r.argv.slice(2)).toEqual(['python', 'agent.py', '--prompt', 'hello world', 'a;b']);
  });

  it('rejects a non-positive or non-numeric timeout', () => {
    for (const t of ['0', '-5', 'abc', '1e3', '10s']) {
      const r = buildCaptureCommand({ commandLine: 'python a.py', level: 'standard', timeoutSeconds: t });
      expect(r.ok, t).toBe(false);
    }
  });

  it('rejects an environment tag outside the offered list', () => {
    const r = buildCaptureCommand({ commandLine: 'python a.py', level: 'standard', environment: 'prod; rm -rf /' });
    expect(r.ok).toBe(false);
  });

  it('rejects an unknown capture level', () => {
    // @ts-expect-error — an invalid value must fail closed at run time too.
    const r = buildCaptureCommand({ commandLine: 'python a.py', level: 'paranoid' });
    expect(r.ok).toBe(false);
  });

  it('never puts a session token in the command', () => {
    for (const p of WORKLOAD_PRESETS.filter((x) => x.commandLine)) {
      const r = buildCaptureCommand({ commandLine: p.commandLine, level: 'standard', outputDir: '/c' });
      expect(r.ok && r.command).not.toMatch(/token/i);
    }
  });

  it('builds a command for every non-empty preset', () => {
    for (const p of WORKLOAD_PRESETS.filter((x) => x.commandLine)) {
      expect(buildCaptureCommand({ commandLine: p.commandLine, level: 'standard' }).ok, p.id).toBe(true);
    }
  });
});

describe('splitCommandLine', () => {
  it('splits plain words and collapses repeated whitespace', () => {
    expect(splitCommandLine('  python   -m  my_agent ')).toEqual({ ok: true, argv: ['python', '-m', 'my_agent'] });
  });

  it('honours single quotes, double quotes and backslash escapes', () => {
    expect(splitCommandLine(`a 'b c' "d \\"e\\"" f\\ g`)).toEqual({ ok: true, argv: ['a', 'b c', 'd "e"', 'f g'] });
  });

  it('keeps an empty quoted argument', () => {
    expect(splitCommandLine(`python a.py ''`)).toEqual({ ok: true, argv: ['python', 'a.py', ''] });
  });

  it.each([
    ['pipe', 'python a.py | tee log'],
    ['and-list', 'make && python a.py'],
    ['sequence', 'python a.py; echo done'],
    ['redirect', 'python a.py > out.txt'],
    ['substitution', 'python $(which agent)'],
    ['variable', 'python $AGENT'],
    ['backtick', 'python `which agent`'],
    ['glob', 'pytest tests/*.py'],
    ['tilde', 'python ~/agent.py'],
    ['comment', 'python a.py #note'],
    ['variable in double quotes', 'python "$AGENT"'],
    ['bracket glob', 'pytest tests/test_x.py::test[a]'],
    ['brace expansion', 'python {a,b}.py'],
    ['history expansion', 'python a.py --msg hi!'],
    ['history expansion in double quotes', 'python a.py --msg "hi!"'],
    ['tilde after = in an argument', 'python a.py --config=~/cfg.yaml'],
    ['tilde in a NAME=~ word', 'python a.py x=~/y'],
    ['tilde after :', 'python a.py PATH=/bin:~/bin'],
    ['zsh =cmd expansion', 'python =agent'],
  ])('refuses shell syntax: %s', (_name, line) => {
    const r = splitCommandLine(line);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toMatch(/shell syntax/);
  });

  it.each([
    ['one assignment', 'OPENAI_API_KEY=x python agent.py'],
    ['two assignments', 'A=1 B=2 python agent.py'],
    ['assignment with quoted value', `MODE='fast' python agent.py`],
  ])('refuses a leading environment assignment: %s', (_name, line) => {
    const r = splitCommandLine(line);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toMatch(/environment assignment/);
  });

  it('keeps NAME=value as a literal argument after the program name', () => {
    expect(splitCommandLine('python agent.py MODE=fast --out=dir/x')).toEqual({
      ok: true, argv: ['python', 'agent.py', 'MODE=fast', '--out=dir/x'],
    });
  });

  it('accepts a quoted bracket, brace, bang or tilde as literal text', () => {
    expect(splitCommandLine(`pytest 'tests/x.py::t[a]' '{a,b}' 'hi!' '~/x' "--c=~/y"`)).toEqual({
      ok: true, argv: ['pytest', 'tests/x.py::t[a]', '{a,b}', 'hi!', '~/x', '--c=~/y'],
    });
  });

  it('accepts a closing bracket or brace on its own as plain text', () => {
    expect(splitCommandLine('python a.py x] y}')).toEqual({ ok: true, argv: ['python', 'a.py', 'x]', 'y}'] });
  });

  it('accepts shell characters inside single quotes as literals', () => {
    expect(splitCommandLine(`python a.py '$HOME | *'`)).toEqual({ ok: true, argv: ['python', 'a.py', '$HOME | *'] });
  });

  it.each([
    ['empty', '   '],
    ['unclosed single quote', "python 'a.py"],
    ['unclosed double quote', 'python "a.py'],
    ['trailing backslash', 'python a.py \\'],
    ['newline', 'python a.py\nrm -rf /'],
    ['control character', 'python a.py\u0007'],
  ])('refuses %s', (_name, line) => {
    expect(splitCommandLine(line).ok).toBe(false);
  });
});
