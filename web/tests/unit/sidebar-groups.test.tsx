/**
 * Sidebar group collapse/expand.
 *
 * The regression this pins: groups default to collapsed, but as soon as you
 * opened a group and clicked an item inside it, that group held the active tab
 * and its header button became a dead no-op — you could expand a group but
 * never collapse it again. Two lines caused it: an `if (activeTabInGroup)
 * return;` guard in `toggleGroup`, and a `&& !groupHasActive` term that forced
 * the group open regardless of stored state.
 *
 * "Never hide the user's current location" is still a real requirement, so the
 * fix keeps it honestly: the active group may be collapsed like any other, and
 * when it is, its header carries a "you are here" marker.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import Sidebar from '@/components/dashboard/Sidebar';

function renderSidebar(tab: Parameters<typeof Sidebar>[0]['tab'] = 'home') {
  return render(
    <Sidebar
      tab={tab}
      onTabChange={vi.fn()}
      counts={{}}
      serverInfo={{ version: '0.101.0', base: 'http://localhost:4321' }}
      onDisconnect={vi.fn()}
      collapsed={false}
      onToggleCollapse={vi.fn()}
      autoRefresh={false}
      onAutoRefreshChange={vi.fn()}
      onHelpOpen={vi.fn()}
    />,
  );
}

/**
 * Group headers only: `getByRole('button', {name: /Compliance/})` is ambiguous
 * because "Compliance" is both a group label and a tab label. Headers are the
 * first child button of each group wrapper in the <nav>.
 */
const groupHeader = (label: string): HTMLElement => {
  const headers = [...document.querySelectorAll<HTMLElement>('nav > div > button:first-child')];
  const found = headers.find((h) => (h.textContent ?? '').trim().startsWith(label));
  if (!found) throw new Error(`no group header starting with "${label}"`);
  return found;
};

beforeEach(() => {
  localStorage.clear();
});

describe('sidebar group collapse', () => {
  it('collapses a group that holds the active tab', async () => {
    const user = userEvent.setup();
    renderSidebar('home'); // 'home' lives in the Overview group

    // The group holding the active tab starts expanded, so its items show.
    expect(screen.getByRole('button', { name: /^Analytics/ })).toBeInTheDocument();

    await user.click(groupHeader('Overview'));

    // THE BUG: previously the click did nothing and the items stayed.
    expect(screen.queryByRole('button', { name: /^Analytics/ })).not.toBeInTheDocument();
  });

  it('re-expands the active group on a second click', async () => {
    const user = userEvent.setup();
    renderSidebar('home');

    await user.click(groupHeader('Overview'));
    expect(screen.queryByRole('button', { name: /^Analytics/ })).not.toBeInTheDocument();

    await user.click(groupHeader('Overview'));
    expect(screen.getByRole('button', { name: /^Analytics/ })).toBeInTheDocument();
  });

  it('marks the collapsed group that holds the active tab', async () => {
    const user = userEvent.setup();
    renderSidebar('home');

    await user.click(groupHeader('Overview'));

    // Location must remain discoverable even though the group is closed.
    expect(within(groupHeader('Overview')).getByTestId('group-active-marker')).toBeInTheDocument();
  });

  it('still toggles a group with no active tab, both ways', async () => {
    const user = userEvent.setup();
    renderSidebar('home');

    // Compliance holds no active tab and starts collapsed.
    expect(screen.queryByRole('button', { name: /^Incidents/ })).not.toBeInTheDocument();

    await user.click(groupHeader('Compliance'));
    expect(screen.getByRole('button', { name: /^Incidents/ })).toBeInTheDocument();

    await user.click(groupHeader('Compliance'));
    expect(screen.queryByRole('button', { name: /^Incidents/ })).not.toBeInTheDocument();
  });

  it('opens the group of a tab reached from outside the sidebar', () => {
    // `g l`, the command palette and ?tab= deep links all change `tab` without
    // any sidebar click. If the destination group stayed closed, the sidebar
    // would highlight nothing at all.
    const { rerender } = renderSidebar('home');

    // Lineage lives in the collapsed 'Provenance & Trust' group.
    expect(screen.queryByRole('button', { name: /^Lineage/ })).not.toBeInTheDocument();

    rerender(
      <Sidebar
        tab="lineage"
        onTabChange={vi.fn()}
        counts={{}}
        serverInfo={{ version: '0.101.0', base: 'http://localhost:4321' }}
        onDisconnect={vi.fn()}
        collapsed={false}
        onToggleCollapse={vi.fn()}
        autoRefresh={false}
        onAutoRefreshChange={vi.fn()}
        onHelpOpen={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: /^Lineage/ })).toBeInTheDocument();
  });

  it('keeps the active group collapsed when the user closes it', async () => {
    // The reveal-on-navigate effect must not fight the user: it keys on `tab`
    // changing, so collapsing the group you are already in has to stick.
    const user = userEvent.setup();
    renderSidebar('home');

    await user.click(groupHeader('Overview'));

    expect(screen.queryByRole('button', { name: /^Analytics/ })).not.toBeInTheDocument();
  });

  it('persists the collapsed set, including the active group', async () => {
    const user = userEvent.setup();
    renderSidebar('home');

    await user.click(groupHeader('Overview'));

    const stored = JSON.parse(localStorage.getItem('novafabric.sidebar-groups') ?? '[]');
    expect(stored).toContain('Overview');
  });
});
