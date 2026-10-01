/**
 * Dashboard polish guards.
 *
 * - Loading and error states are live regions, so assistive tech announces
 *   them (WCAG 4.1.3 Status Messages).
 * - Status colours come from theme tokens, not raw Tailwind palette classes:
 *   `text-amber-500` is ~2:1 on the light theme's background, while
 *   `--color-status-pending` is tuned per theme for WCAG AA. A ratchet, so a
 *   raw status colour cannot creep back into the dashboard.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ErrorBox, Loading } from '@/components/dashboard/helpers';

describe('loading and error states are announced', () => {
  it('Loading is a polite status region', () => {
    render(<Loading />);
    const s = screen.getByRole('status');
    expect(s).toHaveAttribute('aria-live', 'polite');
    expect(s).toHaveTextContent('Loading');
  });

  it('ErrorBox is an alert', () => {
    render(<ErrorBox message="boom" />);
    expect(screen.getByRole('alert')).toHaveTextContent('Error: boom');
  });
});

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap(name => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : p.endsWith('.tsx') ? [p] : [];
  });
}

describe('status colours use theme tokens', () => {
  it('no raw amber status classes in the dashboard', () => {
    const root = join(__dirname, '../../src/components/dashboard');
    const offenders = walk(root).flatMap(file =>
      readFileSync(file, 'utf8')
        .split('\n')
        .map((line, i) => ({ line, i }))
        // Gradients on decorative KPI accents are not status colour.
        .filter(({ line }) => /\b(text|border|bg)-amber-\d{3}\b/.test(line) && !line.includes('from-amber'))
        .map(({ i }) => `${file.slice(root.length + 1)}:${i + 1}`),
    );
    expect(offenders).toEqual([]);
  });
});
