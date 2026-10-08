/**
 * Boots a REAL `nova serve` for the live smoke suite.
 *
 * Unlike tests/e2e (which answers /api/** from canned fixtures against the
 * static preview build), this starts the Python server the dashboard actually
 * ships with, over a throwaway NOVAFABRIC_HOME seeded with real capsules.
 *
 * Safety rules, all enforced here:
 *   - the port is asked from the OS (listen on 0), never fixed;
 *   - every NOVAFABRIC_* / NOVA_* variable inherited from the shell is dropped
 *     and replaced by paths inside a fresh mkdtemp dir, so the live data tree
 *     can never be read or written;
 *   - the server process and the temp dir are always removed in `stop()`.
 *
 * Environment:
 *   NOVA_E2E_PYTHON   interpreter with novafabric installed
 *                     (default: ../../.venv/bin/python, else python3)
 *   PW_CHANNEL        use an installed browser (e.g. "chrome") instead of
 *                     Playwright's bundled Chromium
 */
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

export interface LiveServer {
  base: string;
  token: string;
  home: string;
  stop: () => Promise<void>;
}

/** Why the suite cannot run here, or null when it can. */
export function skipReason(): string | null {
  if (!process.env.PW_CHANNEL) {
    let exe = '';
    try {
      exe = chromium.executablePath();
    } catch {
      /* no bundled browser registered */
    }
    if (!exe || !existsSync(exe)) {
      return 'no Playwright browser installed (run `npx playwright install chromium`, or set PW_CHANNEL=chrome)';
    }
  }
  const py = pythonBin();
  const probe = spawnSync(py, ['-m', 'novafabric.cli.main', '--help'], { encoding: 'utf8' });
  if (probe.status !== 0) {
    return `novafabric is not importable by ${py} (set NOVA_E2E_PYTHON)`;
  }
  return null;
}

export function pythonBin(): string {
  if (process.env.NOVA_E2E_PYTHON) return process.env.NOVA_E2E_PYTHON;
  const venv = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../.venv/bin/python');
  return existsSync(venv) ? venv : 'python3';
}

function freePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const srv = createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      srv.close(() => resolvePort(port));
    });
  });
}

/** Environment for a child: the shell's, minus anything that could point at real data. */
function cleanEnv(home: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (/^(NOVAFABRIC_|NOVA_(?!E2E_))/.test(k) || k === 'FORCE_COLOR' || k === 'COLORTERM') continue;
    env[k] = v;
  }
  env.NOVAFABRIC_HOME = home;
  env.NOVAFABRIC_CAPSULE_DIR = join(home, 'capsules');
  env.NOVAFABRIC_DB_PATH = join(home, 'registry.db');
  env.NOVAFABRIC_SUGGEST = '0';
  return env;
}

async function waitHealthy(base: string, child: ChildProcess, ms: number): Promise<void> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (child.exitCode !== null) throw new Error(`nova serve exited early (code ${child.exitCode})`);
    try {
      const r = await fetch(`${base}/api/health`);
      if (r.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`nova serve did not become healthy within ${ms} ms`);
}

export async function startServer(capsules = 2): Promise<LiveServer> {
  const py = pythonBin();
  const home = mkdtempSync(join(tmpdir(), 'nova-live-e2e-'));
  const env = cleanEnv(home);
  let child: ChildProcess | null = null;
  const stop = async () => {
    if (child && child.exitCode === null) {
      child.kill('SIGTERM');
      await new Promise<void>((done) => {
        const t = setTimeout(() => {
          child?.kill('SIGKILL');
          done();
        }, 5000);
        child?.once('exit', () => {
          clearTimeout(t);
          done();
        });
      });
    }
    rmSync(home, { recursive: true, force: true });
  };
  try {
    const job = join(home, 'job.py');
    writeFileSync(job, 'print("live e2e job")\n');
    for (let i = 0; i < capsules; i++) {
      const cap = spawnSync(py, ['-m', 'novafabric.cli.main', 'capture', '--', 'python3', job], {
        env,
        encoding: 'utf8',
        timeout: 180_000,
      });
      if (cap.status !== 0) throw new Error(`seeding capsule failed: ${cap.stdout}\n${cap.stderr}`);
    }
    const port = await freePort();
    child = spawn(
      py,
      ['-m', 'novafabric.cli.main', 'serve', '--experimental', '--no-browser', '--port', String(port)],
      { env, stdio: 'ignore' },
    );
    const base = `http://127.0.0.1:${port}`;
    await waitHealthy(base, child, 120_000);
    const token = readFileSync(join(home, '.serve-token'), 'utf8').trim();
    return { base, token, home, stop };
  } catch (e) {
    await stop();
    throw e;
  }
}
