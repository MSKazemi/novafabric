/**
 * Navigation invariants — the JS mirror of the Python parity guard
 * (tests/serve/test_command_parity_classification.py), so a breaking change
 * to Sidebar navigation fails locally in vitest before it fails in CI.
 */
import { describe, expect, it } from 'vitest';
import { ALL_TABS, NAV_GROUPS, SHORTCUT_TAB, TAB_SHORTCUT, type Tab } from '@/components/dashboard/Sidebar';
import parity from '@/components/dashboard/commands/commandParity.json';

describe('navigation invariants', () => {
  it('has 29 unique tab ids', () => {
    expect(ALL_TABS.length).toBe(29);
    expect(new Set(ALL_TABS).size).toBe(ALL_TABS.length);
  });

  it('NAV_GROUPS ids are exactly ALL_TABS (no dangling group entries)', () => {
    const groupIds = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.id));
    expect(groupIds).toEqual(ALL_TABS);
  });

  it('every real-panel parity entry points at an existing tab', () => {
    const tabs = new Set<string>(ALL_TABS);
    const bad: string[] = [];
    for (const [cmd, entry] of Object.entries(
      parity as Record<string, { status: string; tab?: string }>,
    )) {
      if (entry.status === 'real-panel' && entry.tab && !tabs.has(entry.tab)) {
        bad.push(`${cmd} -> ${entry.tab}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it('every tab has a unique single-key g-shortcut', () => {
    const keys = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.shortcut));
    expect(keys.length).toBe(ALL_TABS.length);
    expect(new Set(keys).size).toBe(keys.length);
    for (const key of keys) expect(key).toMatch(/^[a-z0-9]$/);
    expect(Object.keys(SHORTCUT_TAB).length).toBe(ALL_TABS.length);
    expect(Object.keys(TAB_SHORTCUT).length).toBe(ALL_TABS.length);
  });

  it('groups stay balanced (2–6 tabs each)', () => {
    for (const g of NAV_GROUPS) {
      expect(g.items.length).toBeGreaterThanOrEqual(2);
      expect(g.items.length).toBeLessThanOrEqual(6);
    }
  });

  it('group labels are unique and non-empty', () => {
    const labels = NAV_GROUPS.map((g) => g.label);
    expect(new Set(labels).size).toBe(labels.length);
    for (const label of labels) expect(label.length).toBeGreaterThan(0);
  });

  it('the Tab union stays assignable from ALL_TABS (compile-time exhaustiveness)', () => {
    // If a tab id were removed from the union but left in NAV_GROUPS (or vice
    // versa) this file stops compiling — tsc is part of `npm run lint`.
    const witness: readonly Tab[] = ALL_TABS;
    expect(witness.length).toBeGreaterThan(0);
  });
});
