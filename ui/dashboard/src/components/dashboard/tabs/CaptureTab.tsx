import { useState, useEffect } from 'react';
import { clsx } from 'clsx';
import { api } from '../../../lib/api';
import type { RunSummary } from '../../../lib/api';

function CmdRow({ cmd, desc }: { cmd: string; desc: string }) {
  return (
    <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 font-mono">
      <div className="text-xs text-[var(--color-text)] break-all">$ {cmd}</div>
      <div className="text-[10px] text-[var(--color-text-muted)] mt-1 font-sans">{desc}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-5 space-y-3">
      <h3 className="text-sm font-medium text-[var(--color-text)]">{title}</h3>
      {children}
    </div>
  );
}

export default function CaptureTab() {
  const [recentRuns, setRecentRuns] = useState<RunSummary[]>([]);

  useEffect(() => {
    api.listRuns().then(r => setRecentRuns(r.runs.slice(0, 5))).catch(() => {});
  }, []);

  return (
    <div className="space-y-4 max-w-3xl">
      {/* Layer C deferred banner */}
      <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-status-pending)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-pending)_8%,transparent)] p-5">
        <div className="flex items-start gap-3">
          <span className="shrink-0 mt-0.5 px-2 py-0.5 rounded text-[10px] uppercase tracking-wider font-medium bg-[color-mix(in_oklab,var(--color-status-pending)_20%,transparent)] text-[var(--color-status-pending)]">
            Layer C — deferred
          </span>
          <div>
            <h3 className="text-base font-medium text-[var(--color-text)]">
              Launching captures from the dashboard is gated by additional review
            </h3>
            <p className="mt-2 text-sm text-[var(--color-text-muted)] leading-relaxed">
              Running <code className="font-mono mx-1 text-[var(--color-text)]">nova capture &lt;user-supplied command&gt;</code>
              from a browser endpoint requires a sandboxed exec model, per-action audit entries, and a verbatim command confirm flow.
            </p>
            <p className="mt-2 text-sm text-[var(--color-text-muted)]">
              <strong className="text-[var(--color-text)]">Layer C may not ship at all</strong> — the security review may conclude the risk outweighs the value. The CLI stays the canonical entry point.
            </p>
          </div>
        </div>
      </div>

      {/* Capture from terminal */}
      <Section title="Capture from your terminal">
        <p className="text-sm text-[var(--color-text-muted)] leading-relaxed">
          The dashboard auto-refreshes the Runs list (toggle in sidebar), so new capsules appear seconds after running them.
        </p>
        <div className="space-y-2 text-sm">
          <CmdRow cmd="nova capture python your_agent.py" desc="wrap a Python script and record every model + tool call" />
          <CmdRow cmd="nova capture --runner docker python your_agent.py" desc="capture inside a container (Docker runner)" />
          <CmdRow cmd="nova capture --runner kubernetes python your_agent.py" desc="capture on a Kubernetes pod (requires kubeconfig)" />
          <CmdRow cmd="nova capture --runner slurm sbatch job.sh" desc="capture a Slurm batch job (sitecustomize injected on compute nodes)" />
          <CmdRow cmd="nova mcp-proxy &lt;upstream-mcp-server&gt;" desc="record MCP tool exchanges into the active capsule" />
          <CmdRow cmd="nova api-proxy --upstream-url https://api.openai.com" desc="record LLM API calls from non-Python clients (HTTP proxy, port 8877)" />
        </div>
      </Section>

      {/* Docker runner */}
      <Section title="Docker runner — all options">
        <p className="text-sm text-[var(--color-text-muted)] leading-relaxed">
          The Docker runner runs the captured workload inside a container via <code className="font-mono mx-0.5 text-[var(--color-text)]">docker run</code>. The container must have novafabric pip-installed. Capsule artifacts are written to a mounted volume.
        </p>
        <div>
          <div className="text-[10px] font-medium uppercase tracking-wider text-[var(--color-text-faint)] mb-2">Starter Dockerfile</div>
          <pre className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 text-xs font-mono text-[var(--color-text)] overflow-x-auto leading-relaxed">{`FROM python:3.11-slim
RUN pip install novafabric
WORKDIR /app
COPY . .`}</pre>
        </div>
        <div className="space-y-2 text-sm">
          <CmdRow
            cmd="nova capture --runner docker --runner-option image=myorg/agent:latest python agent.py"
            desc="required: image — fully-qualified image reference with novafabric installed"
          />
          <CmdRow
            cmd="nova capture --runner docker --runner-option image=myorg/agent:latest --runner-option network=my-net python agent.py"
            desc="attach the container to a named Docker network"
          />
          <CmdRow
            cmd="nova capture --runner docker --runner-option image=myorg/agent:latest --runner-option user=1000:1000 --runner-option workdir=/app python agent.py"
            desc="run as uid:gid and override the container working directory"
          />
          <CmdRow
            cmd="nova capture --runner docker --runner-option image=myorg/agent:latest --runner-option extra_env=OPENAI_API_KEY=sk-… python agent.py"
            desc="inject an extra environment variable (extra_env and extra_volumes are valid CLI flags but not in the command builder UI)"
          />
        </div>
        <p className="text-[11px] text-[var(--color-text-faint)] leading-relaxed">
          Two options not shown in the command builder (multi-value): <code className="font-mono">--runner-option extra_volumes=host:container:opts</code> for additional volume mounts, and <code className="font-mono">--runner-option extra_env=KEY=VALUE</code> to inject extra environment variables beyond NOVAFABRIC_* vars.
        </p>
        <p className="text-[11px] text-[var(--color-text-faint)] leading-relaxed">
          You can also set default runner options in <code className="font-mono">.novafabric/runners.yaml</code> so you don&apos;t need to pass <code className="font-mono">--runner-option</code> on every invocation.
        </p>
        <p className="text-[11px] text-[var(--color-text-faint)] italic">
          Docker runner options (image, network, workdir, user) are surfaced in the Commands tab &rarr; nova capture &rarr; runner=docker.
        </p>
      </Section>

      {/* Distributed / Parent-Child */}
      <Section title="Distributed runs — Parent / Child Capsules (Phase 3, v0.12)">
        <p className="text-sm text-[var(--color-text-muted)] leading-relaxed">
          Multi-process and cluster runs produce a capsule hierarchy: one <strong className="text-[var(--color-text)]">PARENT</strong> capsule per job and one <strong className="text-[var(--color-text)]">WORKER</strong> capsule per rank or process. Typed edge vocabulary links them together (ADR-0044).
        </p>
        <div className="space-y-2 text-sm">
          <CmdRow
            cmd="NOVAFABRIC_GLOBAL_RUN_ID=$(nova new-run-id) nova capture torchrun --nproc-per-node 8 train.py"
            desc="pre-allocate a shared run ID so all ranks write into the same PARENT capsule"
          />
          <CmdRow
            cmd="nova run show --with-children &lt;parent-run-id&gt;"
            desc="view a distributed run's full hierarchy — PARENT + all WORKER capsules"
          />
          <CmdRow
            cmd="nova run validate-distributed &lt;parent-run-id&gt;"
            desc="validate schema, lifecycle state, and typed edge vocabulary for a distributed run"
          />
        </div>
        <p className="text-[11px] text-[var(--color-text-faint)] italic">
          PARTIALLY_COMPLETE state is supported for straggler workers. Dashboard hierarchy view is planned for a future release — use the Runs tab to inspect individual capsules.
        </p>
      </Section>

      {/* What capture records */}
      <Section title="What nova capture records">
        <ul className="space-y-1 text-sm text-[var(--color-text-muted)]">
          <li className="flex items-start gap-2"><span className="shrink-0 text-[var(--color-status-success)]">✓</span> All LLM model calls (request + response, streaming deltas merged)</li>
          <li className="flex items-start gap-2"><span className="shrink-0 text-[var(--color-status-success)]">✓</span> All tool/function calls and results (MCP + function calling)</li>
          <li className="flex items-start gap-2"><span className="shrink-0 text-[var(--color-status-success)]">✓</span> Full OTel GenAI semconv trace (span hierarchy, timing, token counts)</li>
          <li className="flex items-start gap-2"><span className="shrink-0 text-[var(--color-status-success)]">✓</span> Environment snapshot (Python version, packages, GPU/CPU info)</li>
          <li className="flex items-start gap-2"><span className="shrink-0 text-[var(--color-status-success)]">✓</span> Secret scanning + redaction (redaction-proof.json)</li>
          <li className="flex items-start gap-2"><span className="shrink-0 text-[var(--color-status-success)]">✓</span> Exit code, stdout/stderr digest, wall-clock timing</li>
          <li className="flex items-start gap-2"><span className="shrink-0 text-[var(--color-status-success)]">✓</span> Providers: OpenAI, Anthropic, Bedrock, Ollama + aiohttp / urllib3 wire-level</li>
          <li className="flex items-start gap-2"><span className="shrink-0 text-[var(--color-status-success)]">✓</span> NovaSeal signature (DSSE + RFC 3161 timestamp) — opt-in via config (ADR-0041)</li>
        </ul>
      </Section>

      {/* What's available now */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-5 text-sm text-[var(--color-text-muted)] leading-relaxed">
        <p>
          <strong className="text-[var(--color-text)]">What&apos;s available now</strong> (Layer A read-only + Layer B mutations):
        </p>
        <ul className="mt-2 space-y-1 list-disc pl-5">
          <li>browse, inspect, diff, and lineage-query any capsule on disk;</li>
          <li>register an asset from a YAML spec (Registry tab);</li>
          <li>run eval suites against a registered asset (GAIA, SWE-bench, AgentBench, MMLU, TruthfulQA, Smoke);</li>
          <li>promote an asset between lifecycle stages (eval-gated, Rego policy);</li>
          <li>run forensic, dry-run, semantic, or exact replay;</li>
          <li>re-scan a capsule and rewrite its redaction-proof.json;</li>
          <li>build and verify a signed evidence bundle (DSSE + RFC 3161);</li>
          <li>query provenance chains and blast radius in the Lineage tab;</li>
          <li>create and release legal holds (Holds tab);</li>
          <li>test OPA/Rego policy rules interactively (Policy tab).</li>
        </ul>
        <p className="mt-3">
          Every action is audit-logged in{' '}
          <code className="font-mono">~/.novafabric/dashboard-audit.jsonl</code>. See the Audit tab.
        </p>
      </div>

      {/* Recent capsules */}
      {recentRuns.length > 0 && (
        <Section title="Recent capsules">
          <ul className="space-y-2">
            {recentRuns.map(run => (
              <li key={run.run_id} className="flex items-center justify-between gap-3 text-xs font-mono text-[var(--color-text-muted)]">
                <span className="truncate">{run.run_id}</span>
                {run.capsule_path && (
                  <a
                    href={`file://${run.capsule_path}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-[10px] text-[var(--color-accent)] hover:underline shrink-0"
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3">
                      <path d="M2 4a2 2 0 012-2h4l2 2h4a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V4z"/>
                    </svg>
                    Open folder
                  </a>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <AdaptersPanel />
    </div>
  );
}

// ---------- Framework Adapters panel (E-5..E-8 + v0.26.0) ----------

function AdaptersPanel() {
  const [adapters, setAdapters] = useState<Array<{ id: string; module: string; function: string; framework: string; extra: string; available: boolean }>>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.listAdapters()
      .then(d => setAdapters(d.adapters))
      .catch(e => setErr((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-xs font-semibold text-[var(--color-text)]">Framework Adapters</h3>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            Registered Nova capture adapters — install with{' '}
            <code className="font-mono">pip install novafabric[&lt;extra&gt;]</code>
          </p>
        </div>
        <span className="text-[9px] font-mono text-[var(--color-text-faint)] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)]">E-5..E-8</span>
      </div>
      {loading && <p className="text-[10px] text-[var(--color-text-faint)]">Loading…</p>}
      {err && (
        <div className="text-xs text-[var(--color-status-failure)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,transparent)] border border-[color-mix(in_oklab,var(--color-status-failure)_25%,transparent)] rounded px-3 py-2">
          {err}
        </div>
      )}
      {!loading && !err && (
        <div className="grid grid-cols-1 gap-1">
          {adapters.map(a => (
            <div key={a.id} className="flex items-center justify-between text-[10px] py-0.5">
              <div className="flex items-center gap-2">
                <span
                  className={clsx(
                    'w-1.5 h-1.5 rounded-full shrink-0',
                    a.available
                      ? 'bg-[var(--color-status-success)]'
                      : 'bg-[var(--color-border)]',
                  )}
                />
                <span className="font-mono text-[var(--color-text)]">{a.framework}</span>
                {a.available && (
                  <span className="text-[9px] text-[var(--color-status-success)] bg-[color-mix(in_oklab,var(--color-status-success)_10%,transparent)] px-1 py-px rounded">available</span>
                )}
              </div>
              <code className="text-[var(--color-text-faint)]">[{a.extra}]</code>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
