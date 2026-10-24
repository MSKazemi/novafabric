/**
 * Review frontend-dashboard-01 — stored XSS in the Reports chart.
 *
 * Row labels come from capsules (`report_cost_burn` takes `command[0]` from the
 * recorded `capsule.yaml`), and colours from the server's chart catalog. The chart
 * used to string-build its SVG and inject it with `dangerouslySetInnerHTML`, so a
 * capsule whose command was `<img src=x onerror=…>` ran script in the dashboard.
 * The chart now renders React elements: labels are text, colours are attributes.
 */
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import ReportChart, { type ReportChartSpec } from '@/components/dashboard/ReportChart';

const HOSTILE = '<img src=x onerror="window.__xss=1">';

const spec = (kind: ReportChartSpec['kind'], colors = ['#3987e5']): ReportChartSpec => ({
  kind,
  x: 'label',
  y: ['cost'],
  colors,
  title: 'Cost burn',
  requires_filters: [],
});

const rows = [
  { label: HOSTILE, cost: 3 },
  { label: 'python', cost: 5 },
];

describe('ReportChart', () => {
  it.each(['bar', 'stacked-bar', 'line'] as const)(
    'renders a capsule-derived %s label as text, never as markup',
    kind => {
      const { container } = render(<ReportChart spec={spec(kind)} rows={rows} filters={{}} />);
      expect(container.querySelector('img')).toBeNull();
      expect(container.querySelector('[onerror]')).toBeNull();
      expect(container.textContent).toContain('<img src=x');
    },
  );

  it('treats a catalog colour as an attribute value, not markup', () => {
    const evil = '" onmouseover="window.__xss=1';
    const { container } = render(<ReportChart spec={spec('bar', [evil])} rows={rows} filters={{}} />);
    expect(container.querySelector('[onmouseover]')).toBeNull();
    expect(container.querySelector('rect')?.getAttribute('fill')).toBe(evil);
  });

  it('still draws the bars and the axis labels', () => {
    const { container } = render(<ReportChart spec={spec('bar')} rows={rows} filters={{}} />);
    expect(container.querySelectorAll('rect')).toHaveLength(2);
    expect(container.querySelector('svg')?.textContent).toContain('python');
  });
});
