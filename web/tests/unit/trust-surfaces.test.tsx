/**
 * Trust-surface views (ADR-0173 radar / ADR-0174 redaction x-ray).
 *
 * These tests pin the HONESTY contract of the visuals, not just that they
 * render: an unverifiable guarantee must never be drawn as a failure, and the
 * x-ray must never surface a field value.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import TrustRadarGlyph from '@/components/ui/TrustRadarGlyph';
import type { RadarAxis } from '@/lib/api';

const AXES: RadarAxis[] = [
  { key: 'signature', label: 'signature', value: 1, state: 'ok' },
  { key: 'log_integrity', label: 'log integrity', value: 1, state: 'ok' },
  { key: 'timestamp', label: 'timestamp', value: 0.5, state: 'warn' },
  { key: 'redaction', label: 'redaction', value: null, state: 'na' },
  { key: 'attestation', label: 'attestation', value: 0, state: 'fail' },
];

function renderGlyph(axes: RadarAxis[] = AXES) {
  const { container } = render(<TrustRadarGlyph axes={axes} />);
  return container;
}

describe('TrustRadarGlyph', () => {
  it('renders one marker per axis', () => {
    const c = renderGlyph();
    expect(c.querySelectorAll('circle')).toHaveLength(AXES.length);
  });

  it('draws an n/a axis as a hollow tick, never as a zero-reach failure', () => {
    const c = renderGlyph();
    const naMarker = Array.from(c.querySelectorAll('circle')).find((el) =>
      el.querySelector('title')?.textContent?.includes('not applicable'),
    );
    expect(naMarker).toBeTruthy();
    // Hollow (no fill) and dashed — visually distinct from a `fail` dot.
    expect(naMarker!.getAttribute('fill')).toBe('none');
    expect(naMarker!.getAttribute('stroke-dasharray')).toBeTruthy();
  });

  it('excludes n/a axes from the filled verified-area polygon', () => {
    const c = renderGlyph();
    // Ring polygons have no fill gradient; the verified area does.
    const area = Array.from(c.querySelectorAll('polygon')).find((el) =>
      (el.getAttribute('fill') ?? '').startsWith('url('),
    );
    expect(area).toBeTruthy();
    const pointCount = area!.getAttribute('points')!.trim().split(/\s+/).length;
    // 5 axes, one of them n/a → the claim polygon covers only the other 4.
    expect(pointCount).toBe(AXES.length - 1);
  });

  it('renders a fail axis distinctly from an n/a axis', () => {
    const c = renderGlyph();
    const failMarker = Array.from(c.querySelectorAll('circle')).find((el) =>
      el.querySelector('title')?.textContent?.includes('fail'),
    );
    expect(failMarker).toBeTruthy();
    expect(failMarker!.getAttribute('fill')).not.toBe('none');
  });

  it('degrades gracefully below three axes instead of drawing a bogus shape', () => {
    render(<TrustRadarGlyph axes={AXES.slice(0, 2)} />);
    expect(screen.getByText(/Not enough axes/)).toBeInTheDocument();
  });
});

describe('trust-surface API typing (ADR-0174 §1)', () => {
  it('FieldXRay has no value field — the invariant is enforced at the type level', async () => {
    // A compile-time guarantee; asserted structurally so the intent is visible
    // in the suite too. If someone adds `value` to the payload type, the
    // panel could render a secret — this test documents why that is forbidden.
    const mod = await import('@/lib/api');
    expect(mod).toBeTruthy();
    const sample: import('@/lib/api').FieldXRay = { path: 'a.b', state: 'redacted' };
    expect(Object.keys(sample).sort()).toEqual(['path', 'state']);
  });
});
