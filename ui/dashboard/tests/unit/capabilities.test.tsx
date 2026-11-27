/**
 * #283 — capability-aware feature states.
 *
 * The dashboard reads optional-extra state from `/api/doctor` → `extras` and
 * renders one "not available here: why — how to enable" state, with the CLI
 * equivalent, instead of an error after the fact. `unknown` (older server, no
 * metadata, failed call) must never be shown as missing.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const doctor = vi.fn();
const listAdapters = vi.fn();

vi.mock('@/lib/api', () => ({
  api: {
    doctor: (...a: unknown[]) => doctor(...a),
    listAdapters: (...a: unknown[]) => listAdapters(...a),
    listRuns: vi.fn(async () => ({ runs: [] })),
    kgStatus: vi.fn(async () => ({ store: 'kuzu', store_health: 'ok', db_path: '/tmp/kg', edge_count: 0 })),
    collectorStatus: vi.fn(async () => { throw new Error('404'); }),
  },
  ServeApiError: class ServeApiError extends Error {},
}));

import {
  FEATURE_EXTRAS,
  adapterFrameworkLabel,
  extraState,
  installCommand,
  resetExtrasCache,
} from '@/lib/capabilities';
import FeatureUnavailable from '@/components/ui/FeatureUnavailable';
import SigstoreSignPanel from '@/components/dashboard/tabs/seal/SigstoreSignPanel';
import SigstoreVerifyPanel from '@/components/dashboard/tabs/seal/SigstoreVerifyPanel';
import KGTab from '@/components/dashboard/tabs/KGTab';
import CaptureTab from '@/components/dashboard/tabs/CaptureTab';
import CollectorCard from '@/components/dashboard/tabs/infra/CollectorCard';
import { ToastProvider } from '@/lib/ToastContext';

/** `[project.optional-dependencies]` keys, read from the real pyproject.toml. */
function realExtras(): Set<string> {
  const toml = readFileSync(join(__dirname, '../../../../pyproject.toml'), 'utf8');
  const start = toml.indexOf('[project.optional-dependencies]');
  expect(start).toBeGreaterThan(-1);
  const rest = toml.slice(start + '[project.optional-dependencies]'.length);
  const end = rest.search(/^\[/m);
  const section = end === -1 ? rest : rest.slice(0, end);
  const names = new Set<string>();
  for (const m of section.matchAll(/^([a-z0-9][a-z0-9_-]*)\s*=\s*\[/gm)) names.add(m[1]);
  return names;
}

const missingSigstore = {
  ok: true,
  checks: [],
  extras: [
    { name: 'sigstore', installed: false, requires: ['sigstore'], missing: ['sigstore'], install: "pip install 'novafabric[sigstore]'" },
    { name: 'scale-kg', installed: false, requires: ['cachetools', 'kuzu'], missing: ['kuzu'], install: "pip install 'novafabric[scale-kg]'" },
  ],
};

beforeEach(() => {
  resetExtrasCache();
  doctor.mockReset();
  listAdapters.mockReset();
});
afterEach(() => resetExtrasCache());

describe('FEATURE_EXTRAS names only real extras', () => {
  it('every gated extra exists in pyproject.toml', () => {
    const real = realExtras();
    // The scanner must actually see the section, or this passes vacuously.
    expect(real.has('serve')).toBe(true);
    for (const extra of Object.values(FEATURE_EXTRAS)) {
      expect(real.has(extra), `novafabric[${extra}] is not a pyproject extra`).toBe(true);
    }
  });

  it('a phantom name is caught by the same check', () => {
    expect(realExtras().has('langgraph')).toBe(false);
  });
});

describe('extraState', () => {
  it('maps installed / missing / not reported', () => {
    expect(extraState(missingSigstore.extras, 'sigstore')).toEqual({
      state: 'unavailable', missing: ['sigstore'], install: "pip install 'novafabric[sigstore]'",
    });
    expect(extraState([{ name: 'sigstore', installed: true }], 'sigstore')).toEqual({ state: 'available' });
    expect(extraState(missingSigstore.extras, 'serve')).toEqual({ state: 'unknown' });
  });

  it('an older server or a source checkout is unknown, never missing', () => {
    expect(extraState(null, 'sigstore')).toEqual({ state: 'unknown' });
    expect(extraState(undefined, 'sigstore')).toEqual({ state: 'unknown' });
    expect(extraState([], 'sigstore')).toEqual({ state: 'unknown' });
  });

  it('falls back to the quoted install command when the row has none', () => {
    const s = extraState([{ name: 'sigstore', installed: false }], 'sigstore');
    expect(s).toEqual({ state: 'unavailable', missing: [], install: installCommand('sigstore') });
    expect(installCommand('sigstore')).toBe("pip install 'novafabric[sigstore]'");
  });
});

describe('FeatureUnavailable', () => {
  it('says not available, why, how to enable and the CLI equivalent in words', () => {
    render(
      <FeatureUnavailable
        feature="Sigstore keyless signing"
        reason="the sigstore extra is not installed on this server"
        enable="pip install 'novafabric[sigstore]'"
        cli="nova seal sign <capsule_dir>/manifest.json --backend sigstore"
      />,
    );
    const note = screen.getByRole('note', { name: /Sigstore keyless signing: not available here/ });
    expect(note).toHaveTextContent('Not available here:');
    expect(note).toHaveTextContent('Why: the sigstore extra is not installed on this server');
    expect(note).toHaveTextContent("How to enable pip install 'novafabric[sigstore]'");
    expect(note).toHaveTextContent('CLI equivalent nova seal sign');
  });
});

describe('Seal tab: Sigstore panels', () => {
  it('shows the unavailable state and disables Sign when the server reports the extra missing', async () => {
    doctor.mockResolvedValue(missingSigstore);
    render(<SigstoreSignPanel runIds={[]} />);
    expect(await screen.findByRole('note', { name: /Sigstore keyless signing: not available here/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign with Sigstore' })).toBeDisabled();
  });

  it('verify panel gets the same state', async () => {
    doctor.mockResolvedValue(missingSigstore);
    render(<SigstoreVerifyPanel runIds={[]} />);
    expect(await screen.findByRole('note', { name: /Sigstore bundle verification: not available here/ })).toBeInTheDocument();
  });

  it('an older server without `extras` keeps the panel as before', async () => {
    doctor.mockResolvedValue({ ok: true, checks: [] });
    render(<SigstoreSignPanel runIds={[]} />);
    await waitFor(() => expect(doctor).toHaveBeenCalled());
    expect(screen.queryByTestId('feature-unavailable')).toBeNull();
  });

  it('a failing doctor call is unknown, not missing', async () => {
    doctor.mockRejectedValue(new Error('500'));
    render(<SigstoreSignPanel runIds={[]} />);
    await waitFor(() => expect(doctor).toHaveBeenCalled());
    expect(screen.queryByTestId('feature-unavailable')).toBeNull();
  });

  it('the CLI equivalent names the positional argument the command needs', () => {
    doctor.mockResolvedValue({ ok: true, checks: [] });
    render(<SigstoreSignPanel runIds={[]} />);
    expect(screen.getByText(/nova seal sign <capsule_dir>\/manifest\.json --backend sigstore/)).toBeInTheDocument();
  });
});

describe('KG tab', () => {
  it('replaces the panels with one unavailable state when scale-kg is missing', async () => {
    doctor.mockResolvedValue(missingSigstore);
    render(<ToastProvider><KGTab /></ToastProvider>);
    const note = await screen.findByRole('note', { name: /Capsule Knowledge Graph: not available here/ });
    expect(note).toHaveTextContent("pip install 'novafabric[scale-kg]'");
    expect(note).toHaveTextContent('nova kg status');
    expect(screen.queryByText('KG Alias Management')).toBeNull();
  });
});

describe('Capture tab: framework adapters', () => {
  it('renders the server install command and never a novafabric[<extra>] template', async () => {
    doctor.mockResolvedValue({ ok: true, checks: [] });
    listAdapters.mockResolvedValue({
      count: 2,
      adapters: [
        { id: 'langgraph', module: 'm', function: 'wrap', framework: 'LangGraph', extra: null,
          available: true, package: 'langgraph', framework_installed: false, install: 'pip install langgraph' },
        { id: 'a2a', module: 'm', function: 'f', framework: 'A2A SDK', extra: 'a2a',
          available: true, package: 'a2a-sdk', framework_installed: true, install: "pip install 'novafabric[a2a]'" },
      ],
    });
    render(<CaptureTab />);
    expect(await screen.findByText('pip install langgraph')).toBeInTheDocument();
    expect(screen.getByText('framework not installed')).toBeInTheDocument();
    expect(screen.getByText('framework installed')).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('novafabric[<extra>]');
    expect(document.body.textContent).not.toContain('[langgraph]');
  });

  it('labels an older server honestly', () => {
    expect(adapterFrameworkLabel({})).toBe('install state not reported');
  });
});

describe('Infra tab: collector card', () => {
  it('lists novafabric-hpc-hub as future design, not shipped', async () => {
    render(<CollectorCard />);
    const future = screen.getByText(/Future design — not implemented/);
    expect(future.parentElement).toHaveTextContent('novafabric-hpc-hub');
    expect(screen.getByText('Shipped').parentElement).not.toHaveTextContent('novafabric-hpc-hub');
    expect(await screen.findByText(/Collector status is not reported by this server/)).toBeInTheDocument();
  });
});
