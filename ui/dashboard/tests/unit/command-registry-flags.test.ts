/**
 * Review frontend-dashboard-09 — the command builder emitted flags the real CLI does not
 * have (`nova capture --scenario`, `nova verify --verbose`, …) because a curated def
 * replaced the Typer-accurate generated one, and it did not shell-quote values.
 */
import { describe, expect, it } from 'vitest';

import { GENERATED_COMMANDS } from '@/components/dashboard/commands/generatedCommands';
import { buildCommandString, COMMANDS, type CommandDef } from '@/components/dashboard/commands/commandRegistry';

const generated = new Map(GENERATED_COMMANDS.map((c) => [c.name, c]));
const flagOf = (f: { key: string; flag?: string }) => f.flag ?? `--${f.key}`;

describe('command registry', () => {
  it('keeps no flag the real CLI lacks, for any command that has a generated def', () => {
    const offenders: string[] = [];
    for (const cmd of COMMANDS) {
      const real = generated.get(cmd.name);
      if (!real) continue;
      const known = new Set(real.fields.filter((f) => !f.positional).map(flagOf));
      for (const f of cmd.fields) {
        if (f.positional || f.runnerOption) continue;
        if (!known.has(flagOf(f))) offenders.push(`${cmd.name} ${flagOf(f)}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('still lists the commands it curated, with their copy', () => {
    const capture = COMMANDS.find((c) => c.name === 'nova capture');
    expect(capture).toBeTruthy();
    expect(capture!.fields.some((f) => flagOf(f) === '--runner')).toBe(true); // curated copy kept
    expect(capture!.fields.some((f) => flagOf(f) === '--output-dir')).toBe(true); // real flag appended
    expect(capture!.fields.some((f) => flagOf(f) === '--scenario')).toBe(false);
  });
});

describe('buildCommandString', () => {
  const cmd: CommandDef = {
    id: 'x', name: 'nova x', journey: 'debug', description: '', docsPath: '/',
    fields: [
      { key: 'note', label: 'note', type: 'text', hint: '', flag: '--note' },
      { key: 'target', label: 'target', type: 'text', hint: '', positional: true },
    ],
  };

  it('quotes a value with spaces or shell metacharacters', () => {
    expect(buildCommandString(cmd, { note: 'two words; rm -rf /', target: 'a b' })).toBe(
      "nova x --note 'two words; rm -rf /' 'a b'",
    );
  });

  it('escapes a single quote inside a value', () => {
    expect(buildCommandString(cmd, { note: "it's" })).toBe("nova x --note 'it'\\''s'");
  });

  it('leaves plain values bare', () => {
    expect(buildCommandString(cmd, { note: 'plain-1.2/x', target: 'cap_01' })).toBe(
      'nova x --note plain-1.2/x cap_01',
    );
  });
});
