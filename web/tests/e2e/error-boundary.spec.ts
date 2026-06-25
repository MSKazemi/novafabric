import { test, expect } from '@playwright/test';
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ErrorBoundary from '../../src/components/ui/ErrorBoundary';

// D2a — verify the ErrorBoundary contract directly.
//
// `react-dom/server` does NOT exercise componentDidCatch recovery (SSR rethrows
// a child error instead of rendering the fallback). So the happy path is tested
// via SSR, and the error path is tested by driving the boundary's own
// React lifecycle API: getDerivedStateFromError + render(). This tests the real
// component logic without depending on a DOM reconciler.

interface BoundaryLike {
  state: { error: Error | null };
  props: { label?: string; children?: unknown };
  render: () => ReactElement;
}

// Walk a React/JSX element tree collecting text + key attributes for assertions.
// Handles both real React elements and Playwright's `__pw_type: 'jsx'` wrappers
// (both expose `.props`), so it works regardless of the active JSX runtime.
function collect(node: unknown, out: { text: string[]; role: string[]; onClick: boolean[] }): void {
  if (node == null || node === false) return;
  if (typeof node === 'string' || typeof node === 'number') { out.text.push(String(node)); return; }
  if (Array.isArray(node)) { node.forEach((n) => collect(n, out)); return; }
  if (typeof node === 'object' && 'props' in (node as object)) {
    const props = (node as { props?: Record<string, unknown> }).props ?? {};
    if (typeof props.role === 'string') out.role.push(props.role);
    if (typeof props.onClick === 'function') out.onClick.push(true);
    collect(props.children, out);
  }
}

test.describe('ErrorBoundary (D2a)', () => {
  test('renders children unchanged when nothing throws (SSR)', () => {
    const html = renderToStaticMarkup(
      createElement(ErrorBoundary, { label: 'Runs', children: createElement('div', null, 'healthy tab') }),
    );
    expect(html).toContain('healthy tab');
    expect(html).not.toContain('Reload view');
  });

  test('getDerivedStateFromError captures the error into state', () => {
    const err = new Error('kaboom from a tab');
    const next = (ErrorBoundary as unknown as {
      getDerivedStateFromError: (e: Error) => { error: Error | null };
    }).getDerivedStateFromError(err);
    expect(next.error).toBe(err);
  });

  test('fallback shows the section label, message, alert role, and a reload button', () => {
    // Construct the boundary instance and put it in the caught-error state, then
    // call render() to inspect the fallback tree.
    const instance = new (ErrorBoundary as unknown as new (p: object) => BoundaryLike)({
      label: 'Runs',
      children: createElement('div', null, 'should be hidden'),
    });
    instance.state = { error: new Error('kaboom from a tab') };

    const tree = instance.render();
    const out = { text: [] as string[], role: [] as string[], onClick: [] as boolean[] };
    collect(tree, out);
    const joined = out.text.join(' ');

    expect(joined).toContain('Runs');            // guarded-section label surfaced
    expect(joined).toContain('kaboom from a tab'); // error message shown
    expect(joined).toContain('Reload view');       // reset affordance present
    expect(out.role).toContain('alert');           // a11y
    expect(out.onClick.length).toBeGreaterThan(0); // reload button is wired
  });
});
