# Developer Guide

This guide is for **contributors working inside the NovaFabric tree** — not end users.
It documents how to build, test, and extend the code that produces NovaFabric's two
top-level artifacts (the **Run Capsule** and the **Evidence Bundle**) and the five
primitives around them (Asset Registry, Run Capsule, Replay, Lineage, Evidence Bundle).

If you are *using* the `nova` CLI rather than modifying it, start with
[`docs/cli-reference.md`](cli-reference.md) instead.

## What you will learn

- How to set up a local dev environment and run the quality gates (tests, ruff, mypy).
- The repeatable extension patterns for the parts of the codebase you are most likely
  to touch: **asset types, CLI commands, `nova serve` endpoints, report formats, and
  framework adapters**.
- How to work on the internal subsystems that back the trust and lineage layers —
  the NovaSeal signing code, the maker-checker `promote` package, the Event Envelope
  wire format, the standard outer envelopes, and the Security & Provenance Knowledge
  Graph (SPKG).
- The extension points (protocols and entry-point groups) that let you plug in signing
  backends, watcher backends, eval suites, and lineage backends without forking core.

> **Maturity note.** NovaFabric follows a strict docs-honesty rule: every feature is
> either *works today*, *experimental*, *planned*, or *future design*. Many subsystems
> described here (NovaSeal, the Go collector tier, topology dashboards, object capsule
> store) are **in-tree engineering work toward capabilities that are not part of the
> released local-first product surface**. Sections that document such work are marked
> **experimental** or reference the ADR that tracks their design intent. When a section
> is unmarked, it covers a shipped surface. Never describe an experimental or planned
> subsystem as a stable product feature in user-facing docs — see
> [the docs-honesty rule](#docs-honesty-and-maturity-labels) below.

## Contents

- [Local installation](#local-installation)
- [Quality gates](#quality-gates) — tests, ruff, mypy
- **Common extension patterns**
  - [Adding a new asset type](#adding-a-new-asset-type)
  - [Adding a new CLI command](#adding-a-new-cli-command)
  - [Adding a new dashboard tab or input](#adding-a-new-dashboard-tab-or-input)
  - [Adding a new `nova serve` API endpoint](#adding-a-new-nova-serve-api-endpoint)
  - [Adding a new report format](#adding-a-new-report-format)
  - [Extending failure attribution](#extending-failure-attribution-diagnose-adr-0084)
  - [Adding a framework adapter](#adding-a-framework-adapter)
  - [Adding a compliance audit profile](#adding-a-compliance-audit-profile)
  - [Adding a compliance exporter](#compliance-exporters--adding-a-new-format)
- **Trust and provenance subsystems**
  - [Working with NovaSeal](#working-with-novaseal-trustnovaseal) *(experimental)*
  - [Working with the Promote package](#working-with-the-promote-package-promote)
  - [Working with EventEnvelope](#working-with-eventenvelope-envelope)
  - [Standard outer envelopes](#standard-outer-envelopes-envelopes--experimental) *(experimental)*
  - [Working with the SPKG](#working-with-the-spkg-kgspkg--experimental) *(experimental)*
  - [Standalone trust primitives (Python-API only)](#standalone-trust-primitives-python-api-only) *(experimental)*
- **Scale-out and topology work** *(engineering toward planned architecture)*
  - [Building the collector tier](#building-the-collector-tier-go-phase-2)
  - [Live Topology Dashboard development](#live-topology-dashboard-development)
  - [TV-5 3D Topology View development](#tv-5-3d-topology-view-development)
  - [Warm capture daemon development](#warm-capture-daemon-development-daemon-adr-0092)
- **Pluggable extension points**
  - [Signing / KMS, notifier, compression, lineage, and spool backends](#extension-points)
  - [Extending the Capsule Knowledge Graph](#extending-the-capsule-knowledge-graph)
  - [Extending CapsuleWatcher backends](#extending-capsulewatcher-backends)
  - [Registering a third-party eval suite adapter](#registering-a-third-party-eval-suite-adapter)
  - [Writing a custom PII masker](#writing-a-custom-pii-masker-adr-0135--experimental) *(experimental)*
  - [Querying and extending PolicyStore](#querying-and-extending-policystore)
- [Docs-honesty and maturity labels](#docs-honesty-and-maturity-labels)
- [Where to go next](#where-to-go-next)

---

## Local installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:novafabric/novafabric.git
cd novafabric
uv sync --dev
```

The `novafabric` CLI is then available as:

```bash
uv run novafabric --help
# or with the venv activated:
source .venv/bin/activate
novafabric --help
```

### Dependency tiers (ADR-0222)

`[project.dependencies]` is the **default install** — keep it small, and keep it
matching what a plain `pip install novafabric` genuinely needs. As of v0.99.0 it
is 113 MB / 42 packages; `duckdb`, `pyarrow`, `python-louvain` and
`clickhouse-connect` live in extras because every one of their import sites is
already behind an extra.

Two rules bind new code:

1. **Do not add a top-level `import duckdb` / `import pyarrow` /
   `import community` / `import clickhouse_connect` to a core code path.**
   `tests/packaging_metadata/test_lean_install_surface.py` pins the exact set of modules
   allowed to need those, and fails on anything new. Prefer a function-level
   (lazy) import.
2. **Honour the degradation contract.** Any core-reachable use of an
   extras-only dependency must either fall back to a stdlib-equivalent path with
   identical results, or raise an `ImportError` naming the exact extra
   (`pip install novafabric[<extra>]`). Never a silent wrong answer, never a
   bare `ModuleNotFoundError`. See
   `cost/clickhouse_store.py::require_clickhouse_connect` for the house pattern,
   and `tests/packaging_metadata/test_optional_dependency_degradation.py` for how each
   clause is tested.

`networkx` stays in core: the CLI imports it eagerly at start-up and `nova
insights` / `nova lineage` depend on it. Note `nova insights` uses networkx's
own `nx.community.louvain_communities`, *not* the `python-louvain`
distribution (`import community`) that moved to `[serve]`.

The dev venv installs everything (`uv sync --all-extras`), so a missing-extra
bug will not reproduce locally — that is exactly why the two packaging test
modules simulate a lean install with a `sys.meta_path` blocker.

### First-run setup (pip install path)

After installing from source or PyPI, run `nova init` once to create the
data directories and signing keypair:

```bash
nova init                  # creates ~/.novafabric/{capsules,keys,replays}
                           # and ~/.novafabric/keys/signing_key.pem (mode 600)
nova init --force          # regenerate keypair if needed
nova init --home /data/nova  # custom NOVAFABRIC_HOME
```

This is not needed for docker-compose deployments — `make dev-up` handles
all first-boot setup inside the container.

### Docker-compose deployment targets

**Works today.** The self-hosted stack is driven by a small set of `make`
targets (all use `deploy/docker/docker-compose.yml`; the container name
`novafabric-serve` is fixed, so every target manages the same stack and Postgres
volume). After a healthy start each target prints the dashboard URL with a live
token.

| Target | What it does |
|--------|--------------|
| `make dev-up` | Build + start Postgres and `novafabric-serve` only (fast). |
| `make prod-up` | Full stack: + ClickHouse, NATS, Kafka, PgBouncer, JanusGraph. |
| `make update` | `git pull` → rebuild the `nova` image → rolling restart (databases untouched). Use for a normal release deploy from the tracked branch. |
| `make deploy-local` | Build + run `novafabric-serve` straight from the **current working tree** — no `git pull`, no remote round-trip. Stamps the deployed commit (`+ -dirty` when the tree has uncommitted changes) so "what's running" stays answerable. The fast dev-iteration path. |

A repo-root `.dockerignore` keeps the build context lean (it excludes `.git`,
`.venv` and `node_modules`), so source-tree rebuilds stay fast.

> **Deploy checkout tip:** point the deploy checkout's `origin` at the
> authoritative repo you actually commit to, so `make update` fast-forwards
> cleanly. A mirror whose history is force-regenerated will diverge and force a
> manual reset each time.

---

## Quality gates

Run all four gates before opening a PR. Coverage must stay **≥ 90%**, and ruff, mypy
and the documentation link check must be clean. These are the same commands enforced
in CI (see also `CONTRIBUTING.md`).

```bash
make test-fast     # tests (~90 s)
make lint          # ruff check src tests scripts
make typecheck     # mypy src
make check-links   # every relative link in a public doc resolves
```

`make check-links` is not a style gate. It rejects a link whose target is **not
tracked by the public git**, which is a different question from whether the file
exists — this repository keeps one working tree and two gits, so `design/`,
`.claude/`, `CLAUDE.md` and `THREAT_MODEL.md` are all present on your disk and
absent for every reader of the published repository. Checking existence passes
locally and fails only for the person the docs are written for.

### Tests

```bash
uv run pytest --cov=novafabric --cov-report=term-missing
```

Run a focused subset with `-k`:

```bash
uv run pytest -k test_validate -v
```

### Ruff (lint)

```bash
uv run ruff check src tests
uv run ruff check --fix src tests   # auto-fix safe issues
```

### mypy (types)

```bash
uv run mypy src
```

mypy is configured with `strict = true` in `pyproject.toml`. All type errors must
be resolved before merging.

---

## Adding a new asset type

The Asset Registry supports seven asset types (model, agent, prompt, tool, dataset,
evaluation, deployment). To add an eighth:

1. Add a new value to `AssetType` in `src/novafabric/spec/models.py`.
2. Add `<Type>SpecFields` and `<Type>Spec` Pydantic models following the existing
   pattern (see `ModelSpec`, `DatasetSpec`, etc.).
3. Add the new spec class to the `AssetSpec` union discriminator in `models.py`.
4. Add a test fixture in `tests/fixtures/<type>-basic.yaml`.
5. Add a test in `tests/test_spec_validator.py` asserting the fixture validates.

## Adding a new CLI command

1. Create `src/novafabric/cli/<command>.py` with a `<command>_cmd` function.
2. Register it in `src/novafabric/cli/main.py` with `app.command(...)`.
3. Add at least two CLI tests in `tests/test_cli.py`: a success path and an error path.
4. Regenerate the dashboard command registry so the CommandsTab stays a complete
   mirror of the CLI:
   ```bash
   uv run python web/scripts/gen-command-registry.py
   ```
   `tests/serve/test_command_registry_coverage.py` re-runs this introspection in
   CI and fails if the checked-in `generatedCommands.ts` drifts from the live
   Typer app — a forgotten regeneration is a test failure, not a silent gap.
5. If the new command deserves a real dashboard panel (not just a copy-only
   command builder), add a row for it in
   `web/src/components/dashboard/commands/commandParity.json`
   (`"status": "real-panel"` + `tab`/`api`); otherwise it defaults to
   `"builder-only"` and needs no entry.

**Fixed-value options — use `str` Enum types.** When an option accepts a finite set
of values, define a `class MyOption(str, Enum)` in the same module and use it as the
type annotation. This gives shell tab-completion (via `nova --install-completion`) and
exits 2 with a clear error on invalid input instead of a runtime failure. See
`src/novafabric/cli/capture.py` (`RunnerName`) or `src/novafabric/cli/replay.py`
(`ReplayMode`) for the canonical pattern.

After any CLI change, verify the help text matches the implementation:

```bash
uv run nova --help
uv run nova <command> --help
```

## Adding a new dashboard tab or input

The dashboard is a static Astro/React app under `web/`. After editing source files,
rebuild and copy the bundle with the single dedicated script — **not** a plain
`astro build` followed by a manual `cp -r`, which would overwrite sibling
static directories (e.g. `topology/`) that other build steps own:

```bash
cd web && npm run build:dashboard   # astro build + scripts/copy-dashboard.mjs
# or, from the repo root:
make bundle
```

`copy-dashboard.mjs` copies only the entries the web build owns (`_astro`,
`dashboard`, `concepts`, `showcase`, etc. — see the script for the full list)
into `src/novafabric/serve/static/`, leaving other targets' output untouched.

### Tests (v0.97.0)

The dashboard has its own test tiers — run them before rebuilding the bundle:

```bash
cd web
npm run lint        # tsc --noEmit, strict
npm run test:unit   # vitest + jsdom (primitives, hooks, nav invariants)
npx playwright test tests/e2e --reporter=line
```

> **On a machine already running `nova serve` on the Playwright port**, the
> config's `reuseExistingServer` will silently test *that* server's (possibly
> stale) bundle instead of your working tree. Pass `PW_PORT=<free port>` to get
> a clean one. CI sets `reuseExistingServer: false` and is unaffected.

`tests/unit/nav.test.ts` mirrors the Python command-parity guard
(`tests/serve/test_command_parity_classification.py`) in JS, so a navigation
change fails locally in vitest before it fails in CI.

### Design-system primitives (v0.97.0)

Build UI from `web/src/components/ui/primitives/` rather than raw Tailwind
strings — Button, Input, Select, Textarea, Field, Card, Badge, StatusPill,
SegmentedControl, Modal, Drawer, Tooltip, Toolbar, and `Icon` (a semantic
wrapper over lucide-react, so the icon set is swappable in one file). Colors,
elevation, spacing, and motion come from `web/src/styles/tokens.css`; **10px
(`--text-2xs`) is the minimum type size** — smaller text fails contrast checks.

| Component | Purpose |
|---|---|
| `primitives/*` | The design system — prefer these over hand-rolled markup. |
| `DataTable.tsx` | Virtualized table (TanStack Virtual) with sort, sticky header, `onEndReached` infinite scroll, and a `footer` slot. Use it for any list that can grow. |
| `TruncationNotice.tsx` | The ADR-0199 honesty affordance — "Showing N of ~M — load more". **A bounded list must never truncate silently.** |
| `SuggestInput.tsx` | Text input with a live-filtered suggestion dropdown. Accepts `suggestions: string[]`; shows up to 8 on focus, filters as you type. Click-outside and Escape close the dropdown; Enter fires the optional `onEnter` callback and bubbles so parent `<form>` `onSubmit` still triggers. Use this instead of a bare `<input>` whenever the field accepts a database reference (run ID, asset `name@version`, registry name, etc.). |
| `CopyButton.tsx` | Copy-to-clipboard button with a transient "copied" state. |

**Data fetching**: `lib/useQuery.ts` (reads), `lib/useMutation.ts` (writes), and
`lib/usePaginatedQuery.ts` — one hook covering both server pagination models
(ADR-0199 keyset cursors and offset/limit) that also surfaces the
`total`/`approximate`/`truncated` honesty signals for `TruncationNotice`.

### Tab structure (v0.97.0)

Tabs are decomposed: each large tab is a thin shell over a per-tab directory
(`tabs/compliance/`, `tabs/runs/`, `tabs/registry/`, `tabs/infra/`, `tabs/seal/`,
`tabs/admin/`, `tabs/kg/`, `tabs/governance/`). Add a panel as its own file, not
as another section inside the shell. `dashboard/PanelScaffold.tsx` owns the
repeated load → pending → error → result chrome; use it instead of re-writing
that state machine. Compliance additionally has a manifest (`tabs/compliance/index.ts`)
and a `?sub=` hub — a new panel is one file plus one manifest entry.

**Navigation invariants** (`Sidebar.tsx`): the `export type Tab = …` union is
parsed *textually* by the Python parity guard — keep it a single-quoted
string-literal union, and never move it to another file. Regrouping `NAV_GROUPS`
is free; renaming a tab id requires updating `commandParity.json` in lockstep.
Each tab declares a stable `g`-sequence shortcut key (`g h`, `g r`, …) —
shortcuts are per-tab, not positional, so reordering the sidebar is safe.

**Adding a new text input that references database data:**

1. Import `SuggestInput` from `../../ui/SuggestInput` (adjust path).
2. Load the suggestion pool from the server (if not already in scope) — `api.listAssets()`, `api.listRuns()`, or `api.listHolds()`.
3. Map to `string[]`: e.g. `assets.map(a => \`${a.name}@${a.version}\`)`.
4. Replace the bare `<input type="text">` with `<SuggestInput value={...} onChange={...} suggestions={pool} className={...} />`.
5. Rebuild and copy the bundle (see above).

## Adding a new `nova serve` API endpoint

Every serve endpoint mirrors an equivalent `nova` CLI command — the CLI + JSON surface
is canonical, and the dashboard is a thin read-only view over it. All serve endpoints
follow the same three-part pattern:

1. **Resolve the capsule** using `_resolve_capsule(run_id, capsule_dir)` — raises HTTP 404 if the directory doesn't exist.
2. **Require auth** with `dependencies=[Depends(verify_token)]` on the route decorator.
3. **Mirror a CLI command** — every endpoint should have an obvious CLI equivalent noted in its docstring.

```python
@app.get("/api/example/{run_id}", dependencies=[Depends(verify_token)])
async def example_endpoint(run_id: str) -> dict[str, Any]:
    """Mirror of `nova example <run_id>`."""
    cdir = _resolve_capsule(run_id, capsule_dir)
    # ... do work using cdir ...
    return {"run_id": run_id, "result": ...}
```

Add tests in `tests/test_serve_app.py` using the `client` fixture — three tests minimum: auth required (no token → 401), unknown run → 404, happy path → expected shape.

The matching dashboard panel goes in that tab's directory — `web/src/components/dashboard/tabs/<tab>/<Panel>.tsx` — and is rendered by the tab shell (see *Tab structure* above). Add a `Dashboard equivalent:` note in `docs/cli-reference.md` under the corresponding CLI command, and if the command now has a real panel, upgrade its `commandParity.json` entry from `builder-only` to `real-panel` with its `tab` and `api` (the guard checks the `api` string appears in **both** `web/src/lib/api.ts` and `src/novafabric/serve/`).

## Adding a new report format

`generate_report(format_, db_path=None)` in `src/novafabric/report/generator.py`
dispatches on `format_` with a chain of early-return `if format_ == "<name>":`
checks (currently `json`, `html`, falling through to markdown as the default).
Binary formats that need to render the HTML first (e.g. `pdf` via WeasyPrint,
see `generate_report_pdf`) live as a separate function rather than returning
from `generate_report` itself.

1. Add a new `if format_ == "<name>": return ...` branch (or a sibling
   `generate_report_<name>()` function for a binary format) in `generator.py`.
2. Add the value to the `ReportFormat` enum in `src/novafabric/cli/report.py`
   and dispatch on it in `report_cmd` (mirror the `pdf` branch if the format is
   binary and needs `--output`).
3. Add a test in `tests/test_report.py` asserting the output is valid for the
   format, and a CLI test for the new `--format` value.

## Extending failure attribution (diagnose/, ADR-0084)

`src/novafabric/diagnose/` is a pure, read-only analysis layer over the captured
trace plus the lineage store. `attribute_failure(capsule_dir, lineage_store=None)`
returns a `RunAttribution` (ranked `list[StepAttribution]`, chosen responsible step,
and an `AgentErrorTaxonomy` label).

To extend it:

1. **Add a taxonomy cue.** Append keyword cues to the appropriate entry in
   `_TAXONOMY_CUES` in `attribution.py`. Cues are matched case-insensitively against
   the step's error text + name; the first matching category wins, so order matters
   (SYSTEM is checked before ACTION to catch infrastructure faults).
2. **Add a scoring signal.** In the coarse pass of `attribute_failure`, add a bounded,
   additive term to `score`. Keep terms small and bounded (no unbounded weights) and
   keep the function deterministic for a given capsule.
3. **New taxonomy category.** Extend the `AgentErrorTaxonomy` enum — it is a public,
   stable surface, so adding a member is additive but removing/renaming one is a
   breaking change (update ADR-0084 and the CLI reference if you do).
4. **Test first.** Add a fixture in `tests/test_attribution.py` (a multi-step failed
   run where the responsible step is known) asserting the rank and label.

Do **not** write attribution results back into the lineage store from this module —
it is read-only by design (ADR-0084). Scores are relative ranking weights, never
presented as calibrated probabilities.

**Related, newer modules in `diagnose/` (ADR-0101, experimental):**

- `verify.py::verify_hypothesis` / `search_root_cause` back the CLI's
  `nova diagnose --intervene` and `--search-root-cause` flags — they replay the
  capsule counterfactually (ADR-0086, mocked/zero-token) and record an
  evidence-based `CONFIRMED`/`REFUTED`/`INCONCLUSIVE` verdict rather than a
  guess.
- `causal_graph.py::causal_root_candidates` (NF-019/022) back-traces the span
  graph to root failure nodes (a failing node with no failing ancestor). It has
  **no CLI verb of its own** — it's consumed internally by
  `search_root_cause`. Every candidate carries `verification="unverified"`
  until an intervention replay confirms it.
- `claim_audit.py::audit_claims` (NF-021) marks a model-span claim `ungrounded`
  when no tool-span evidence precedes it on the answer path — a structural
  hallucination-risk signal, no NLP. **This is Python-API only today** — it is
  not wired into `nova diagnose`'s output or any CLI command. If you add a CLI
  surface for it, follow the "Adding a new CLI command" pattern above and
  update `docs/user-guide.md`'s v0.75–v0.94 cohort table to move it from
  "Python API only" to "CLI".

## Adding a compliance audit profile

Compliance profiles live in `src/novafabric/compliance/audit/profiles/` as YAML files.
Each file defines a list of evidence checkers:

```yaml
# src/novafabric/compliance/audit/profiles/my-standard.yaml
profile_id: my-standard
name: My Standard v1.0
version: "1.0"
checkers:
  - id: MS-01
    name: Capsule Schema Present
    weight: 1.0
    required_fields:
      - capsule_id
      - schema_version
  - id: MS-02
    name: Seal Present
    weight: 2.0
    required_fields:
      - seal
```

Add the profile ID to the `--profile` option help text in `src/novafabric/cli/audit.py`.
Run `nova audit map --profile my-standard` to confirm it loads.

> **Compliance-honesty reminder.** Audit profiles and exporters produce *evidence that
> supports* a compliance workflow. NovaFabric attests only that a capsule is unmodified
> since signing; it does not certify or guarantee compliance with any regulation
> (EU AI Act, NIST AI RMF, ISO/IEC 42001, GDPR, HIPAA, FDA 21 CFR Part 11, SOC 2, etc.).

## Compliance exporters — adding a new format

Compliance exporters live in `src/novafabric/compliance/export/`. Each exporter
follows a two-method pattern:

```python
class MyExporter:
    def build_report(self, capsule_dir: Path) -> MyReport:
        """Build the report model from capsule files."""
        ...

    def export_json(self, report: MyReport, output_path: Path) -> Path:
        """Serialise to JSON and return the written path."""
        ...
```

**Wiring up a new exporter:**

1. Create `src/novafabric/compliance/export/my_format.py` with the two-method class.
2. Add a CLI command in `src/novafabric/cli/export_evidence.py` (see `export_ropa_cmd` as a template).
3. Register the CLI command in `src/novafabric/cli/main.py`.
4. Add a `nova serve` endpoint in `src/novafabric/serve/app.py` following the `compliance_export_ropa_endpoint` pattern — import the exporter inside the function to avoid unconditional heavy imports.
5. Add an `api.ts` method in `web/src/lib/api.ts` (see `exportRopa` as a template).
6. Add a `<MyFormatExportPanel>` component as its **own file** under `web/src/components/dashboard/tabs/compliance/` (built on `PanelScaffold`), then register it in the manifest at `tabs/compliance/index.ts` with its `group` — the hub renders whatever the manifest declares. (Before v0.97.0 these panels lived inline in `ComplianceTab.tsx`; that file is now just the sub-navigation shell.)
7. Write integration tests in `tests/test_serve_compliance.py` — one happy-path test and one 422 on missing `run_id`.

**Existing exporters for reference:**

| Exporter | File | Cap | Dashboard panel |
|---|---|---|---|
| `GDPRRoPAExporter` | `compliance/export/gdpr_ropa.py` | cap-007 | `RoPAExportPanel` |
| `AIBOMExporter` | `compliance/export/aibom.py` | cap-008 | `AIBOMExportPanel` — CycloneDX 1.7 |
| `NISTAIRMFReporter` | `compliance/export/nist_rmf.py` | cap-009 | `NISTRMFExportPanel` |
| `AnnexIVExporter` | `compliance/export/annex_iv.py` | cap-002 | `AnnexIVPanel` |
| `NIS2Exporter` | `compliance/export/nis2.py` | cap-005 | `NIS2Panel` |
| `ROCrateExporter` | `compliance/export/ro_crate.py` | — | `RoCrateExportPanel` |

---

## Working with NovaSeal (trust/novaseal/)

> **Maturity: experimental / engineering toward planned architecture.** NovaSeal is the
> in-tree signing subsystem tracked by ADR-0041. It is *not* part of the shipped
> local-first product surface as a supported feature; interfaces here may change. The
> shipped trust primitive is the ed25519-signed **Evidence Bundle** built by
> `nova export-evidence`, verifiable offline with only `sha256sum` plus an ed25519
> verifier.

The NovaSeal signing code lives entirely in `src/novafabric/trust/novaseal/`.
Its tests are in `tests/seal/`. All tests in that directory run fast (no network
calls — TSA requests are mocked).

**Running NovaSeal tests only:**

```bash
uv run pytest tests/seal/ --benchmark-disable -v
```

**Running the p99 latency gate** (enforced in CI, ~1 s locally):

```bash
uv run pytest tests/seal/test_benchmark.py -v
# or:
make benchmark
```

The gate (`test_seal_p99_latency_gate`) runs 100 `pedantic` rounds of
`NovaSeal.seal()` and asserts that nearest-rank p99 < 200 ms.  It skips
automatically when `--benchmark-disable` is active so it does not slow down
normal `make test` runs.  Results are written to
`.benchmark-results/seal_latency.json` by `make benchmark` and saved as a
90-day artifact in the `seal-latency-gate` CI job.

**Adding a new signing profile** (e.g. Sigstore keyless — planned):

1. Add a new `profile:` value to `SigningProfile` in `config.py`.
2. In `config.py._parse_profile`, add validation for the new profile's required fields.
3. In `envelope.py.create_envelope`, dispatch on `profile` for the key-loading path.
4. Add fixtures in `tests/seal/` for the new profile.
5. Write an ADR if this changes the public wire format (DSSE envelope schema).

**Using the Postgres Merkle log backend:**

Install the optional extra:

```bash
pip install novafabric[seal-postgres]
```

Point `NOVAFABRIC_SEAL_DB_PATH` at a Postgres DSN (or set `merkle_db` in `novaseal.yaml`):

```bash
export NOVAFABRIC_SEAL_DB_PATH=postgresql://user:pass@host:5432/nova
nova seal log verify          # sampled check, p99 < 200 ms at 1M entries
nova seal log verify --full   # full O(N) re-hash audit
```

`NovaSeal.__init__` calls `open_merkle_log(db_path)` which dispatches by URI prefix:
- `Path` or non-DSN string → `MerkleLog` (SQLite, default)
- `postgresql://` or `postgres://` prefix → `PostgresMerkleLog` (psycopg3)

Tables are self-bootstrapped on first connection (`CREATE TABLE IF NOT EXISTS`).
No Alembic migration is needed — the schema is stable by design (append-only Merkle log).
Tests for the Postgres backend are in `tests/seal/test_postgres_merkle.py`, gated
behind `NOVA_INTEGRATION=1` + `NOVA_TEST_POSTGRES_DSN`.

**Seal bundle location convention:**

All sealing code writes to `<capsule_dir>/.seal/`:
- `manifest.dsse` — DSSE envelope bytes (UTF-8 JSON)
- `manifest.dsse.tsr` — RFC 3161 TSR bytes (DER binary; empty if TSA skipped)
- `log-entry.json` — Merkle log entry (JSON, human-readable)

---

## Working with the Promote package (promote/)

The maker-checker linked-envelope chain lives in `src/novafabric/promote/`.
Tests are in `tests/promote/`. Test key fixtures are at `tests/fixtures/promote/keys/`
(ECDSA P-256, committed with `git add -f` despite the `*.pem` gitignore rule — test-only keys, not production credentials).

**Running promote tests only:**

```bash
uv run pytest tests/promote/ -v --tb=short
```

**Module responsibilities:**

| Module | Responsibility |
|---|---|
| `predicates.py` | DSSE sign/verify with promote payload types; predicate builders; JSON Schema validation via `jsonschema`; JCS-based `proposal_digest` |
| `policy_store.py` | SQLite `promote_policy` table in the NovaSeal Merkle log DB; `get_by_version`, `get_active_at`, `get_latest`, `put` |
| `bundle_store.py` | Filesystem proposal/approval DSSE bundle CRUD; paths `{data_dir}/promote/{capsule_id}/proposal/{uuid}.json` |
| `verifier.py` | `verify_sod()` + `VerifyResult` dataclass; five checks with distinct exit codes 3–7 |
| `exceptions.py` | `PredicateValidationError`, `PolicyNotFoundError`, `BundleNotFoundError`, `SoDError` |

**DSSE payload types used:**

```python
PROPOSAL_PAYLOAD_TYPE = "application/vnd.novafabric.promote.proposal+json"
APPROVAL_PAYLOAD_TYPE = "application/vnd.novafabric.promote.approval+json"
POLICY_PAYLOAD_TYPE   = "application/vnd.novafabric.promote.policy+json"
```

These are distinct from the single-signer NovaSeal `PAYLOAD_TYPE` (`application/vnd.novafabric.capsule+json`). The `verify_promote_envelope()` function in `predicates.py` checks the payload type; do not use `verify_envelope()` from `trust/novaseal/envelope.py` for promote bundles.

**Adding a new predicate type (e.g. `promote/bypass/v1`):**

1. Add a JSON Schema to `src/novafabric/schemas/promote_bypass_v1.json` (already present — schema exists, CLI deferred to a later sprint).
2. Add `BYPASS_PAYLOAD_TYPE` to `predicates.py`.
3. Add `build_bypass_predicate()` to `predicates.py`.
4. Add CLI command in `src/novafabric/cli/seal_propose.py` under `seal_app`.
5. Add tests in `tests/promote/`.

**Smoke test (local, no network):**

```bash
KEYS=tests/fixtures/promote/keys
TMPDIR=$(mktemp -d)
uv run nova policy sign --key $KEYS/admin.pem --cert $KEYS/admin_cert.pem \
  --proposer-subjects "proposer" --approver-subjects "approver" --db "$TMPDIR/m.db"
UUID=$(uv run nova seal propose test-capsule-001 \
  --justification "Sprint 1 smoke test: model v1.0 validation complete." \
  --key $KEYS/proposer.pem --cert $KEYS/proposer_cert.pem \
  --db "$TMPDIR/m.db" --data-dir "$TMPDIR" | grep "Proposal created:" | awk '{print $3}')
echo y | uv run nova seal approve "$UUID" --capsule-id test-capsule-001 \
  --key $KEYS/approver.pem --cert $KEYS/approver_cert.pem \
  --db "$TMPDIR/m.db" --data-dir "$TMPDIR"
uv run nova seal verify test-capsule-001 --db "$TMPDIR/m.db" --data-dir "$TMPDIR"
# Expected: SoD verification passed
```

---

## Working with EventEnvelope (envelope/)

The Event Envelope v1 wire format lives in `src/novafabric/envelope/`.
Its tests are in `tests/test_event_envelope.py`.

**Running Event Envelope tests only:**

```bash
uv run pytest tests/test_event_envelope.py -v
```

**Adding a new event_type:**

1. Add the new string value to `EventType` in `envelope/models.py`.
2. Add the same string to the `"enum"` array in `schemas/event-envelope-v1/envelope-v1.json`.
3. Recompute `schemas/event-envelope-v1/envelope-v1.sha256`:
   ```bash
   python3 -c "
   import hashlib, pathlib
   c = pathlib.Path('schemas/event-envelope-v1/envelope-v1.json').read_bytes()
   if c.endswith(b'\n'): c = c[:-1]
   pathlib.Path('schemas/event-envelope-v1/envelope-v1.sha256').write_text(hashlib.sha256(c).hexdigest())
   "
   ```
4. Update `envelope-v1.proto` with the same new string in a comment next to `event_type`.
5. Add tests in `tests/test_event_envelope.py` for the new event_type.

**Changing the envelope schema (adding an optional field):**

Adding an optional field is backwards-compatible (additive). Steps:
1. Add the field to `schemas/event-envelope-v1/envelope-v1.json` with `"oneOf": [{"type": ...}, {"type": "null"}]` (optional) or add it to `"required"` (new required field — only add new required fields before any consumers depend on the current schema).
2. Add the field to `envelope-v1.proto` with the next available tag number (alphabetical order).
3. Add a Pydantic field to `EventEnvelope` in `envelope/models.py` with a default of `None`.
4. Recompute the sha256 pin (see above).
5. Update this file and [`docs/architecture.md`](architecture.md).

**Breaking changes require `envelope_version: "2"`** — removing fields, changing types, or making optional fields required. These are forbidden in the v1.x line. Write an ADR before proceeding.

**Schema hash pin enforcement:**

Downstream consumers embed the sha256 in their schema references. A mismatch between the expected and actual hash is a build-time error. Always recompute the pin after editing `envelope-v1.json`.

**CloudEvents interop (gap-009, ADR-0081):**

To route envelopes through a CloudEvents-aware broker (Kafka/NATS/HTTP) without
adding NovaFabric-specific parsing, use the structured-mode mapping in
`src/novafabric/envelope/cloudevents.py`:

```python
from novafabric.envelope import EventEnvelope, to_cloudevents, from_cloudevents

ce = to_cloudevents(envelope)          # plain JSON-serializable dict (CloudEvents v1.0)
envelope2 = from_cloudevents(ce)       # round-trips back to an equivalent EventEnvelope
```

`to_cloudevents()` always emits the four required CloudEvents context attributes
(`id`, `source`, `type`, `specversion="1.0"`). The mapping is **additive** — the
internal `EventEnvelope` model is untouched and the schema pin does not change.

Key contract (mirrors `collector/pkg/envelope/cloudevents_mapping.go` so
Python↔Go interop holds over the same topic):

| EventEnvelope field | CloudEvents attribute |
|---|---|
| `event_id` | `id` |
| `agent_id` | `source` (`nova://agent/{agent_id}`) |
| `event_type` | `type` |
| `started_at` | `time` |
| `trace_id` + `span_id` | `traceparent` extension (`00-{trace}-{span}-01`) |
| `global_run_id` / `run_id` | `novarunglobal` / `novarun` extensions |
| `parent_run_id` / `cluster_id` / `tenant_id` / `emitter_node_id` / `payload_hash` | `novaparentrun` / `novacluster` / `novatenant` / `novaemitter` / `novapayloadhash` (omitted when null) |
| `nova.batch.signature` / `nova.batch.signing_key_id` | `novabatchsig` / `novabatchkid` |
| `payload` | `data` |

Extension attribute names are lowercase `[a-z0-9]` per the CloudEvents naming
rule (no hyphens or dots). Unknown broker-injected extension attributes are
preserved across a `from_cloudevents()` → `to_cloudevents()` round-trip (stashed
under the model's `cloudevents_extensions` extra block). Tests:
`tests/test_envelope_cloudevents.py`.

> **Out of scope (Wave 2):** SCITT COSE Receipts — the other half of gap-009 —
> are not yet implemented.

---

## Standard outer envelopes (envelopes/) — experimental

The `src/novafabric/envelopes/` package (NF-029/030/031, ADR-0096, **experimental**) wraps
NovaFabric's inner artifacts in stock, third-party-verifiable envelopes — **wrap, don't replace**:
the inner bytes become the envelope payload verbatim and are never rewritten. Tests live in
`tests/envelopes/`.

| Module | Emitter | Envelope |
|---|---|---|
| `envelopes/dsse.py` | `wrap_bundle(bundle_bytes, signer)` | DSSE, `payloadType application/vnd.novafabric.bundle+json` |
| `envelopes/intoto.py` | `capsule_statement(capsule_dir, …)` | in-toto Statement v1, `predicateType novafabric.dev/capsule/v1` (per-file sha256 subjects) |
| `envelopes/slsa.py` | `promotion_provenance(…)` | in-toto Statement, `predicateType https://slsa.dev/provenance/v1` |

**Single DSSE writer (do not fork).** All three emitters sign through the one PAE + signer
implementation in `evidence/intoto.py` — `dsse_sign_payload(payload, payload_type, signer)` (and its
statement-level wrapper `dsse_sign(statement, signer)`). A `signer` is any object exposing
`sign(bytes) -> bytes` and `keyid: str` (e.g. `evidence/signing.py::LocalSigner`, or the keyring-key
adapter in `cli/promote.py`). **Never add a second DSSE code path** (requirement 2 of ADR-0096).

**Digest fidelity.** `capsule_statement(..., expected_digests=...)` raises `SubjectDigestMismatch` if a
recomputed per-file sha256 disagrees with an expected one — the emitter refuses to produce a
verifying-but-wrong attestation.

**Schema conformance.** `envelopes/_schemas/` vendors the in-toto Statement v1 and SLSA Provenance v1
required-field contracts; `envelopes/schema.py::validate_intoto_statement()` /
`validate_slsa_provenance()` assert emitter output against them (raising `EnvelopeSchemaError`), so a
renamed/missing field fails fast instead of producing an envelope a stock verifier would reject. These
JSON files are shipped as wheel package data (`pyproject.toml [tool.hatch.build.targets.wheel].include`) —
add any new vendored schema to that list.

**CLI surfaces (all opt-in; output byte-for-byte unchanged when the flag is absent):**
- `nova export-evidence --dsse` — DSSE-wraps the final bundle `manifest.json` → `<bundle>.dsse.json`.
- `nova promote direct --slsa-provenance` — emits a DSSE-signed SLSA provenance → `<name>-<version>.slsa.json`.
- `nova verify-envelope <env.json> --key <pem>` — verifies any of the above with an Ed25519 key.

> **Three distinct DSSE verify paths now coexist — do not cross them:**
> 1. **`trust/novaseal/envelope.py::verify_envelope()`** — a *capsule's* NovaSeal seal
>    (`application/vnd.novafabric.capsule+json`; also checks RFC 3161 + Merkle). Used by `nova verify`.
> 2. **`promote/predicates.py::verify_promote_envelope()`** — maker-checker promote bundles
>    (`…promote.proposal/approval/policy+json`).
> 3. **`cli/verify_envelope.py` (`nova verify-envelope`)** — the *outer* envelopes above
>    (`…bundle+json`, in-toto, SLSA), verifying the signature only via `evidence/intoto.py::dsse_verify`.
>
> They share the DSSE *PAE* encoding but carry different payload types and check different things.
> Pick by artifact: capsule seal → (1); promote bundle → (2); standard outer envelope → (3).

**Adding a new outer envelope type:** add an emitter module under `envelopes/` that builds the
statement/payload and signs via `dsse_sign`/`dsse_sign_payload` (never a new signer), add tests under
`tests/envelopes/`, and — if it introduces a user-facing surface — wire an opt-in CLI flag and graduate
the `CAPABILITY_MAP.md` row to `experimental`. Write an ADR only if it changes a public payload type.

---

## Working with the SPKG (`kg/spkg/`) — experimental

The Security & Provenance Knowledge Graph (ADR-0111, spec in private `design/`) turns capsule lineage
into a security-reasoning graph. It has two layers and a detector, all under `src/novafabric/kg/spkg/`:

| Module | Role | Heavy deps? |
|---|---|---|
| `ontology.py` | Namespace IRIs (PROV-O, ATT&CK, D3FEND) + SHACL shapes (incl. `nf:FindingShape`) | rdflib **lazy** — imports without the extra |
| `provo_mapping.py` | `read_lineage_edges()`, `lineage_edge_to_provo()`, `capsule_lineage_to_provo()`, `finding_to_rdf()`, `validate_provo()` | rdflib/pyshacl **lazy** |
| `graph_store.py` | `SpkgGraphStore` — embedded KùzuDB LPG (Entity/EDGE), `attack_path()` | kuzu (eager, needs `[spkg]`) |
| `build.py` | `build_spkg(capsule_dir, store)` — canonical RDF (SHACL-gated) **then** LPG rebuild from the same capsule | via the above |
| `entity_resolution.py` | `EntityResolver` — in-house Fellegi–Sunter cross-vendor linker (stdlib) | none |
| `detect.py` | `StructuralAnomalyDetector` + `to_findings()` — unsupervised edge-level scorer | **none (pure stdlib)** |

CLI surfaces (`cli/kg.py`): `nova kg build-provenance` (RDF export), `nova kg build` (populate both
stores), `nova kg detect` (anomaly scan). The first two need `pip install novafabric[spkg]`; **`detect`
needs no extra**.

Serve/dashboard surface (`serve/app.py`), read-only server-side parity for the CLI, behind the same token
+ localhost host guard as the other `/api/kg/*` routes:
- `POST /api/kg/detect` — mirrors `nova kg detect` (body `{capsule_path, top}` → `{ok, count, findings}`); no extra.
- `POST /api/kg/attack-path` — mirrors `nova kg attack-path` (body `{capsule_path, from_entity, to_entity, max_depth}` → `{ok, path_found, hops}`); needs `[spkg]`.
- `POST /api/kg/blast-radius` — mirrors `nova kg blast-radius` (body `{capsule_path, entity, upstream, max_depth}` → `{ok, direction, count, entities}`); needs `[spkg]`.

All resolve a bare `run_id` to its capsule dir. The dashboard React panels that consume these are a
follow-up built with the `packages/nova-dashboard` toolchain (not part of this Python slice).

**Two hard invariants** — do not break these:

1. **R2 — no bare score.** Every finding MUST carry a MITRE ATT&CK technique and/or a D3FEND
   countermeasure. This is enforced twice: the JSON `spkg-anomaly-finding-v1.schema.json` contract and
   the RDF `nf:FindingShape` SHACL constraint (`sh:or` over `nf:mapsToTechnique`/`nf:mapsToCountermeasure`).
   When you add a detector, emit findings via `to_findings()` / `finding_to_rdf()`; never invent a second
   finding shape.
2. **R11 — SHACL gate before write.** `build_spkg` validates the canonical PROV-O layer *before* touching
   the operational store; on failure it raises `SpkgValidationError` and leaves the LPG untouched. Any new
   ingest path must validate first.

**Keep the detector dependency-free.** The v0.1 `StructuralAnomalyDetector` is deliberately pure-stdlib so
`nova kg detect` runs on base `novafabric`. The PyGOD/TGN GNN upgrade is a **resource-gated** slice — it
pulls torch/torch_geometric, whose wheels bundle third-party components that need a full distribution-
license audit under [ADR-0024]. Do not add it without that audit and an ADR pointer.

**Adding a new finding type or detector:** implement scoring in a new module (or extend `detect.py`),
return findings through `to_findings()`, add tests under `tests/kg/` (assert the injected malicious edge
ranks top-k *and* the finding is schema/SHACL-valid), and — if user-facing — add a `nova kg <verb>`
command mirroring `detect` (import lazily; only guard with the `[spkg]` message if you actually need rdflib
/kuzu). Update `CHANGELOG.md` and `docs/cli-reference.md`.

---

## Standalone trust primitives (Python-API only)

A run of ADR-0101-adjacent ADRs (v0.75.0–v0.94.0) added several small, tested
trust modules that are each **pure Python API, with no `nova` CLI verb and no
dashboard panel**. They don't share a package — each lives next to the
subsystem it extends. Treat each as **experimental**: implemented and tested,
but no shipped end-user workflow wraps it yet. If you add a CLI or dashboard
surface for one, follow the "Adding a new CLI command" pattern above, mirror
the change into `docs/user-guide.md`'s v0.75–v0.94 cohort table, and note it in
`CHANGELOG.md`.

| Module | ADR | What it does |
|---|---|---|
| `trust/novaseal/x509_identity.py` | 0055 | Offline signing identity pinned to an x509 cert's SHA-256 fingerprint (ECDSA-P256/RSA-PSS) — verification checks `fingerprint ∈ pinned` **and** the signature, deliberately skipping CA path-building so it stays a pure local check. |
| `trust/novaseal/hybrid_signature.py` | 0072 | Crypto-agility envelope over a pluggable algorithm registry — Ed25519 today, a post-quantum algorithm (ML-DSA) can register as a second signer later with no envelope format change ("either alone suffices"). |
| `trust/did.py` | 0075 | Self-certifying `did:key` (Ed25519, base58btc + multicodec, no network lookup) plus Verifiable Credential issue/verify. |
| `trust/delegation.py` | 0106 | Signed user → agent → sub-agent "acted-as" delegation chain (`issue_grant`, `verify_delegation_chain`) — scope only ever attenuates down the chain, never escalates. |
| `trust/novaseal/witness.py` | 0097 | Checkpoint + witness cosigning over the existing NovaSeal Merkle log's consistency proofs — anti-split-view; a second party corroborates the log hasn't forked. |
| `compliance/sovereignty.py` | 0077 | Jurisdiction site-seals (`issue_site_seal`/`verify_site_seal`) — an Ed25519 countersignature verified under the key registered for the *claimed* jurisdiction, so a forged residency claim fails verification; `check_cross_jurisdiction_read` enforces a `ResidencyPolicy`. |

None of these modules write to a capsule or mutate shipped schemas — they are
additive, standalone primitives you import directly. Tests live alongside the
existing subsystem's test tree, not a dedicated directory for this group:
`tests/seal/` for the three `trust/novaseal/` modules (`test_x509_identity.py`,
`test_hybrid_signature.py`, `test_witness.py`), `tests/trust/` for `did.py` and
`delegation.py`, and `tests/compliance/test_sovereignty.py` for the site-seal
module.

---

## Building the collector tier (Go, Phase 2)

> **Maturity: engineering toward planned architecture.** The Go collector tier targets
> the cluster-scale ingestion path documented as design intent in ADR-0020/0039. It is
> *not* part of the shipped local-first product surface. The shipped capture path needs
> no collector at all — see the capture data flow in
> [the architecture overview](./architecture.md).

The collector is a separate Go 1.22 module at `collector/`. Install Go:

```bash
# Download Go 1.22 to ~/go/ (no root required)
curl -fsSL https://go.dev/dl/go1.22.12.linux-amd64.tar.gz | tar -xz -C ~
export PATH=$HOME/go/bin:$PATH   # add to ~/.bashrc for persistence
```

Build and test:

```bash
cd collector

# Download dependencies
GOPATH=~/gopath go mod tidy

# Run unit tests with race detector
GOPATH=~/gopath go test -race ./...

# Build all three binaries
GOPATH=~/gopath go build -o bin/novafabric-collector ./cmd/novafabric-collector
GOPATH=~/gopath go build -o bin/novafabric-verifier  ./cmd/novafabric-verifier
GOPATH=~/gopath go build -o bin/novafabric-hpc-hub   ./cmd/novafabric-hpc-hub

# Validate the 1000-event corpus
make spec-test
```

**Key collector packages:**

| Package | Description |
|---|---|
| `collector/pkg/canonical` | ADR-001 deterministic OTLP canonical encoding |
| `collector/pkg/novaseal` | Ed25519 signer, KMS client, LocalWAL dev fallback |
| `collector/pkg/envelope` | EventEnvelope Go types + OTLP / CloudEvents mappings |
| `collector/pkg/metrics` | Prometheus metrics registry |
| `collector/internal/spool` | Lustre-safe rename-commit JSONL spool (cap-001) |
| `collector/internal/processor/novasealbatchsigner` | OTel custom processor (cap-002) |
| `collector/internal/hpc` | NATS leaf lifecycle wrapper |
| `collector/internal/verifier` | Offline Ed25519 batch-signature verifier |

**Development mode (no KMS required):**

```bash
# Use a local dev key instead of the production NovaSeal KMS
export NOVAFABRIC_KMS_LOCAL_WAL=1
./bin/novafabric-collector --config testdata/collector.yaml
```

A WARN is emitted on every signing call when `NOVAFABRIC_KMS_LOCAL_WAL=1`. The local key is stored at `~/.novafabric/dev-keys/<uuid>.pem` (mode 0400). To prevent accidental use in production: the key is refused if `NOVAFABRIC_ENV=production`.

**Adding a new Go package:**

1. Create `collector/pkg/<name>/` or `collector/internal/<name>/`.
2. Write tests in `_test.go` files in the same package.
3. Run `GOPATH=~/gopath go test -race ./...` before committing.
4. Ensure all dependencies are Apache-2.0, MIT, or BSD-3 (per ADR-0024).
5. If the package introduces a new `nova_*` Prometheus metric, register it in `collector/pkg/metrics/metrics.go`.

**HPC integration tests** (require Docker):

```bash
cd collector/tests/integration
go test -tags=integration -v ./...
```

These tests require Docker + `docker compose` for the Slurm-in-Docker and 10-node NATS cluster harnesses. They are skipped in standard CI unless the `integration` build tag is set.

---

## Adding a framework adapter

Framework adapters live in `src/novafabric/adapters/`. Each adapter is a single
module that monkey-patches or wraps the framework's entry-point method to record
`nova capture`-compatible events. NovaFabric is framework-neutral — it *records* what
LangGraph/AutoGen/CrewAI/DSPy/OpenAI Agents SDK do rather than orchestrating them.

Pattern (using `crewai.py` as the model):

```python
# src/novafabric/adapters/myframework.py
from novafabric.capture.event_recorder import get_current_recorder

def wrap_thing(thing):
    original = thing.run

    def _wrapped(*args, **kwargs):
        recorder = get_current_recorder()
        try:
            result = original(*args, **kwargs)
            if recorder:
                recorder.record_event(...)
            return result
        except Exception:
            if recorder:
                recorder.record_event(...)
            raise

    thing.run = _wrapped
    return thing
```

Rules:
1. Fail-open: never raise inside the wrapper; log and swallow.
2. Use `get_current_recorder()` — never instantiate a new recorder.
3. Restore the original method on `unwrap()` if reversibility matters.
4. Add an entry to `src/novafabric/adapters/__init__.py`.
5. Register the adapter in `docs/cli-reference.md` and [`docs/architecture.md`](architecture.md).

### Typed `record_*` methods (extended event taxonomy, ADR-0082)

Beyond the generic recorder, `EventRecorder` exposes typed convenience methods
for the extended span taxonomy (ADR-0082). Each validates a Pydantic model,
appends one line to a dedicated JSONL stream tagged with an `event_type`
discriminator, and is **fail-open** — a bookkeeping failure never reaches the
agent workflow:

| Method | JSONL stream | Event type |
|---|---|---|
| `record_file_event` | `file_events.jsonl` | `FileEvent` |
| `record_network_event` | `network_events.jsonl` | `NetworkEvent` |
| `record_human_approval` | `human_approvals.jsonl` | `HumanApprovalEvent` |
| `record_state_transition` | `state_transitions.jsonl` | `StateTransition` |
| `record_memory_operation` | `memory_operations.jsonl` | `MemoryOperation` |
| `record_guardrail` | `guardrail_events.jsonl` | `GuardrailEvaluated` |
| `record_evaluator` | `evaluator_events.jsonl` | `EvaluatorScored` |
| `record_reranker` | `reranker_events.jsonl` | `RerankerApplied` |
| `record_vector_retrieval` | `vector_retrievals.jsonl` | `VectorRetrieval{Started,Completed,Failed}` (by `phase`) |

```python
recorder = get_current_recorder()
if recorder:
    recorder.record_guardrail("toxicity-filter", outcome="passed", score=0.02)
    recorder.record_vector_retrieval("pgvector", phase="completed", top_k=5, returned_count=5)
```

---

## Live Topology Dashboard development

> **Maturity: engineering toward planned architecture.** The topology dashboard is
> prototype work; a live topology view (`nova serve --topology`) is planned and depends
> on prototype spikes. The shipped serve surface is the local-only, read-only
> `nova serve --experimental` dashboard (Layer A). Do not present topology views as a
> released feature.

The topology dashboard has two separate development loops.

### Python server-side

Topology modules live in `src/novafabric/serve/topology/`. Run tests with:

```bash
uv run pytest tests/test_topology_*.py -v
```

To start the server in topology mode:

```bash
make serve-topology   # build SPA + start nova serve --experimental --topology
```

### TypeScript SPA (`packages/nova-dashboard/`)

```bash
cd packages/nova-dashboard
npm install
npm run dev        # Vite dev server with HMR (proxies API to nova serve)
npm test           # vitest unit tests
npm run build      # production build → dist/
```

After a production build, copy artifacts to the Python static dir:

```bash
make topology-build   # rsync packages/nova-dashboard/dist/ → src/novafabric/serve/static/topology/
```

The SPA talks to the Python server at the same origin. In dev mode, configure
`vite.config.ts` proxy to point at your running `nova serve` instance.

## TV-5 3D Topology View development

> **Maturity: engineering toward planned architecture** — same caveat as the 2D
> topology dashboard above.

TV-5 adds a Three.js 3D topology view alongside the existing 2D Sigma.js view.

### Python server-side (TV-5)

TV-5 modules live in `src/novafabric/serve/topology/`. Run dedicated tests with:

```bash
uv run pytest tests/tv5/ -v
```

The three TV-5 modules are:

- `snapshot_store_3d.py` — `SnapshotStore3D`: atomic fine/coarse snapshot tiers (msgpack or JSON)
- `layout_pipeline_3d.py` — `LayoutPipeline3D`: `networkx.spring_layout` 3D layout in `ProcessPoolExecutor`
- `router_tv5.py` — FastAPI router factory `make_tv5_router()`: `/api/tv5/` REST + WebSocket

To start the server with TV-5 enabled:

```bash
nova serve --experimental --tv5
# Or with the 2D topology dashboard too:
nova serve --experimental --topology --tv5
```

Environment variables:

- `TV5_SNAPSHOT_DIR` — storage path (default `.nova/tv5_snapshots`)
- `TV5_MAX_FINE_SNAPSHOTS` — retention count for fine tier (default 288)
- `TV5_MAX_COARSE_SNAPSHOTS` — retention count for coarse tier (default 168)

The optional `msgpack` package enables binary snapshot transport. Without it, JSON is used automatically.

### TypeScript SPA (TV-5)

TV-5 adds one new component to the nova-dashboard SPA:

- `src/components/topology/TV5Panel.tsx` — Three.js `InstancedMesh` per node type, `LineSegments` edges, `OrbitControls`, time-slider, WS live feed, p99 health color encoding

The existing `src/App.tsx` has a tab switcher: **2D View** (Sigma.js, existing) / **3D View (TV-5)** (Three.js, new).

```bash
cd packages/nova-dashboard
npm install    # also installs three, @react-three/fiber, @react-three/drei
npm test       # includes TV5Panel.test.ts
npm run build  # produces static bundle including TV5Panel
```

---

## Warm capture daemon development (daemon/, ADR-0092)

The warm capture daemon (`src/novafabric/daemon/`) is a **prefork** `AF_UNIX`
server: the parent imports `novafabric` once, then `os.fork()`s one worker per
run. User-facing docs are in [`docs/warm-capture-daemon.md`](warm-capture-daemon.md);
this section is for working on the daemon itself.

Module layout:

| File | Responsibility |
|---|---|
| `protocol.py` | length-prefixed JSON frames + SCM_RIGHTS fd passing (server/worker side) |
| `client.py` | **stdlib-only** thin client (`novacap`); must not import the `novafabric` package |
| `server.py` | forking server: peercred check, request dispatch, fork, SIGCHLD reaper |
| `worker.py` | post-fork: dup2 stdio, `setpgrp`, run `CaptureOrchestrator`, cancel watcher |

Hard rules when editing here:

- **The prefork model depends on `import novafabric` starting no background
  threads.** A fork after a thread is started corrupts the child. There is a gate
  test for this — `tests/daemon/test_fork_safety_preflight.py`. If you add an
  import-time thread anywhere in the import graph, that test fails and the daemon
  is unsafe; make the thread lazy instead.
- **`client.py` must stay stdlib-only.** Importing it must not pull in the
  `novafabric` package, or the thin client re-acquires the cold-start the daemon
  exists to remove. It carries its own small copies of the frame helpers. There is
  no test that mechanically enforces this — keep its imports to the standard
  library by hand.
- **`run_worker` runs only in a forked child.** It rebinds stdio, calls
  `setpgrp`, and may `killpg` its own process group — never call it in-process. It
  is exercised for real (via an explicit `os.fork`) in
  `tests/daemon/test_worker_integration.py` and end-to-end in
  `tests/daemon/test_fidelity_e2e.py`; fork/loop-only branches are marked
  `# pragma: no cover` with a pointer to those tests.
- **Fidelity is the contract.** The worker must run the existing
  `CaptureOrchestrator` unchanged so a daemon capsule stays structurally identical
  to a direct one. `tests/daemon/test_fidelity_e2e.py` enforces this — keep it green.

To extend the protocol (e.g. a new `op`), add it in `server._handle` (the parent
reads the first frame and decides whether to fork) and mirror any client-side
framing in `client.py`. Liveness probes are a bare connect that the parent reads as
`None` and closes — they must never fork.

---

## Extension points

These are the plug-in surfaces that let you integrate NovaFabric with your own
infrastructure **without forking core**. Each is a `typing.Protocol` or an entry-point
group with a stable contract.

### BypassNotifier — bypass event dispatch

Implement the `BypassNotifier` Protocol (`src/novafabric/promote/bypass_notify.py`) to
route maker-checker bypass events to any destination:

```python
from novafabric.promote.bypass_notify import BypassEvent, BypassNotifier

class SlackBypassNotifier:
    def notify(self, event: BypassEvent) -> None:
        # post to Slack; never raise
        ...
```

Wire it via `PromoteBundleStore(notifier=SlackBypassNotifier())` or set
`NOVA_BYPASS_NOTIFY_FILE` / `NOVA_BYPASS_NOTIFY_WEBHOOK` for the built-in backends.

### SigningBackend — Cloud KMS signing

Implement the `SigningBackend` Protocol (`src/novafabric/trust/novaseal/signing_backend.py`)
to integrate any key management system:

```python
from novafabric.trust.novaseal.signing_backend import SigningBackend

class VaultSigningBackend:
    def sign(self, payload: bytes) -> bytes: ...
    def public_key_pem(self) -> str: ...
    @property
    def algorithm(self) -> str: ...
```

Pass the backend to `create_envelope(capsule, backend=VaultSigningBackend())`.
Built-in cloud backends: `AwsKmsSigningBackend`, `AzureKvSigningBackend`,
`GcpKmsSigningBackend` — activated via `build_signing_backend(profile)`.

Install the relevant optional extra:
- `pip install novafabric[seal-aws]` — AWS KMS (boto3)
- `pip install novafabric[seal-azure]` — Azure Key Vault
- `pip install novafabric[seal-gcp]` — GCP Cloud KMS

### ZstdDictRegistry — OCS compression dictionaries

Use `ZstdDictRegistry` (`src/novafabric/object_capsule_store/zstd_dict.py`) to train
and register named zstd dictionaries for capsule compression:

```python
from novafabric.object_capsule_store import ZstdDictRegistry

reg = ZstdDictRegistry()
reg.train(dict_id="model-weights-v1", samples=[sample1, sample2, ...])
client = ObjectCapsuleStoreClient(..., zstd_registry=reg)
await client.put_capsule(capsule, compression_dict_id="model-weights-v1")
```

Install with `pip install novafabric[ocs-compress]`.

### JanusGraph lineage backend

Use `JanusGraphLineageStore` for cluster-scale lineage storage:

```python
from novafabric.lineage.backends.janusgraph import JanusGraphLineageStore

store = JanusGraphLineageStore(url="ws://janusgraph:8182/gremlin")
store.insert(from_ref="run-a", to_ref="run-b", edge_type="depends_on", meta={})
chain = store.provenance("run-b", depth=3)
```

LDBC SNB BI queries via `LineageSnbQueries`; deploy the included Helm chart at
`deploy/helm/janusgraph/`. Install with `pip install novafabric[janusgraph]`.

> **Note.** SQLite is the default local-mode lineage store and needs no network; it is
> the source-of-truth-derived, rebuildable cache. Alternative backends like this one are
> strictly additive — local mode never requires them.

### AGE lineage backend — docker-compose profile (experimental)

Use `AGELineageStore` (`src/novafabric/lineage/backends/age.py`) for the Apache AGE
(openCypher-over-Postgres) lineage backend — a third behavioural peer of
`SqliteLineageStore`/`PostgresLineageStore`/`KuzuLineageStore`, correctness-parity-tested
against the SQLite reference via testcontainers (`tests/lineage/test_age_backend.py`):

```python
from novafabric.lineage.backends.age import AGELineageStore

store = AGELineageStore(dsn="postgresql://nova:nova@localhost:5433/nova_lineage")
```

**Experimental:** a dedicated `age` docker-compose profile lets you run Apache AGE
locally without pulling in testcontainers:

```bash
make age-up      # docker compose --profile age up -d age — starts ONLY novafabric-age on :5433
make age-down     # docker compose --profile age stop age && rm -f age — removes ONLY that container
```

Both targets name the `age` service explicitly on every command and never call bare
`up`/`down` — `docker compose --profile age config --services` resolves to the union
`{age, postgres, nova}` (the latter two have no `profiles:` key, so they're always
"active" under any profile filter), and a bare `up`/`down` would start or tear down
that whole union. Naming `age` explicitly scopes every command to that one container;
`make age-up`/`make age-down` never touch an already-running dev/prod stack's
`novafabric-postgres` or `novafabric-serve` (verified live).

DSN: `postgresql://nova:nova@localhost:5433/nova_lineage`. `AGELineageStore.__init__`
self-initializes the extension (`CREATE EXTENSION IF NOT EXISTS age`) and the graph
(`create_graph`) on first connect — no manual setup or init-SQL mount step is required.
Connecting from Python needs the `[server]` extra for `psycopg[binary]`:
`pip install novafabric[server]`.

> **Note.** This is a genuinely separate Postgres instance from the MetadataStore's
> `postgres` service (port 5432) — the AGE extension is not present in plain
> `postgres:16-alpine`, and the lineage graph is a derived/rebuildable artifact that
> deliberately does not share the metadata database. This docker-compose profile is
> **experimental** and strictly opt-in — it is absent from both the default dev stack
> and `prod`.

`make age-down` deliberately does not pass `-v`, so the named `age-data` volume
(`docker_age-data`, or `<project>_age-data` if you've overridden the compose project
name) is preserved across restarts — the same convention as `dev-down`/`prod-down`
preserving `pg-data`/`kuzu-data`. To discard the AGE data volume entirely (e.g. after
experimenting with the backend), remove it explicitly and intentionally:
`docker volume rm docker_age-data` (confirm the exact name first with
`docker volume ls | grep age-data` — the prefix depends on your compose project name).

### NovaPySpool — Python cffi spool binding

`NovaPySpool` (`src/novafabric/collector_cffi/spool.py`) provides a Python-native spool
compatible with the Go collector's binary segment format. When `libnovaspool.so` is
present it binds via cffi; otherwise it falls back to pure-Python atomic rename:

```python
from novafabric.collector_cffi.spool import NovaPySpool

spool = NovaPySpool(spool_dir=Path("/var/nova/spool"), max_files=1000)
spool.write(b'{"event":"capture","ts":1234567890}')
segments = spool.drain()  # returns list[Path] of committed segment files
```

There is no `pip install`-able extra for this — `libnovaspool.so` is a Go shared
library built out-of-band by a separate Go toolchain step (see
`src/novafabric/collector_cffi/spool.py`'s module docstring for the `go build`
invocation), not something `pyproject.toml` can express or produce. The `cffi`
binding layer itself needs no extra either: it comes in transitively via the
core `cryptography` dependency on CPython, so `NovaPySpool` is importable out
of the box. When the `.so` isn't present (or fails to load) it automatically
falls back to the pure-Python atomic-rename backend — no extra install step
is required either way.

---

## Extending the Capsule Knowledge Graph

The KG schema has 5 node tables and 4 relationship tables.  To add a new node type:

1. Add `CREATE NODE TABLE IF NOT EXISTS <Type> (id STRING PRIMARY KEY, …)` to
   `KGStore._ensure_schema()` in `kg/store.py`.
2. Add a `merge_<type>()` method and the corresponding upsert logic.
3. Register the type in `EntityNormaliser.normalise()` dispatch (`kg/entity_normaliser.py`).
4. Add extraction logic in `KGIngestionPipeline._resolve_entities()` (`kg/pipeline.py`).
5. Include the new type in `count_nodes()` / `get_topology_graph()` in `kg/store.py`.
6. Add the type to `KGTopology` in `web/src/lib/api.ts` and update `TopologyLayerPanel`
   in `web/src/components/dashboard/tabs/KGTab.tsx`.

**MCP server auto-detection pattern:** tool names containing `:` are
split on the first `:` in `_resolve_entities()`.  The left part becomes the `MCPServer`
name; the right part is the `Tool` name.  A `SERVED_BY` (Tool → MCPServer) edge is added
in addition to the standard `USES_TOOL` (Agent → Tool) edge.  Follow this pattern when
adding other auto-detection heuristics.

### `nova kg alias register --type mcp_server`

The alias table accepts `mcp_server` as an entity type (alongside `model`, `agent`,
`tool`, `endpoint`).  Register a display-name alias for a raw MCP server prefix:

```bash
nova kg alias register "filesystem" "nova-mcp-filesystem" --type mcp_server
```

### `nova kg query` MCP server output

`nova kg query <agent-id>` includes a `mcp_servers` key in the JSON result and
prints a "MCP servers reachable" section in text mode.  The 2-hop path is
`Agent → Tool → MCPServer` via `USES_TOOL` + `SERVED_BY`.

### KG auto-ingest configuration

The `nova serve` background loop polls for new capsules.  Two environment variables
control its behaviour:

| Variable | Default | Description |
|---|---|---|
| `NOVA_KG_INGEST_INTERVAL` | `60` | Poll interval in seconds |
| `NOVA_KG_PATH` | `.nova/kg/nova_kg.kuzu` | KuzuDB file path |

The set of already-ingested capsule directories is persisted in a SQLite sidecar
(`ingest_tracker.db`) so restarts don't re-process the entire history.  The tracker
is implemented in `src/novafabric/kg/ingest_tracker.py` (`IngestTracker` class).

### Topology cache

`GET /api/kg/topology` caches its response for 30 seconds.  The cache is
process-local (not shared across workers) and is invalidated on process restart.
Dashboard `TopologyLayerPanel` is lazy-loaded — it fetches only when the user clicks
"Load topology", not on panel mount.

## Extending CapsuleWatcher backends

`CapsuleWatcher` (`src/novafabric/serve/capsule_watcher.py`) abstracts capsule
directory scanning behind `_BackendProtocol` (a `typing.Protocol`):

```python
import sqlite3
from pathlib import Path

class MyCustomBackend:
    """Example: watch an NFS mount or remote S3-prefix list."""

    @property
    def name(self) -> str:
        return "my-custom"

    def poll_once(self, capsule_dir: Path, conn: sqlite3.Connection) -> int:
        """Scan for new capsules and upsert them into runs_cache. Return count ingested."""
        ...

    def close(self) -> None:
        """Release resources (inotify watches, file handles, etc.)."""
        ...
```

Instantiate `CapsuleWatcher(backend=MyCustomBackend())` and call
`watcher.ingest_all()` / `watcher.poll_once()` / `watcher.start_background()`.

Built-in backends:

| Backend | Module | How selected |
|---|---|---|
| `PollingBackend` | `capsule_watcher.py` | default (`auto` when watchdog absent) |
| `WatchdogBackend` | `capsule_watcher.py` | `NOVA_WATCHER_BACKEND=watchdog` or `--backend watchdog`; requires `pip install novafabric[watch]` |

`nova serve` instantiates `CapsuleWatcher` with the backend resolved from
`NOVA_WATCHER_BACKEND` (default: `auto`).  The poll interval is controlled by
`NOVA_WATCHER_INTERVAL` (default: `2.0` s).

## Registering a third-party eval suite adapter

Eval suites are discovered at runtime via the `novafabric.eval_suites` entry-point group. To ship a custom suite in your own package, implement `EvalSuiteAdapter` and register it:

```python
# my_package/my_suite.py
from pathlib import Path
from novafabric.evals.adapter import EvalSuiteAdapter
from novafabric.evals.result import EvalResult

class MySuiteAdapter:
    def suite_id(self) -> str:
        return "my-suite-v1"

    def version(self) -> str:
        return "1.0.0"

    def oci_digest(self) -> str:
        return ""  # "" = host-env; "sha256:<hex>" = OCI-pinned

    def run(self, capsule_path: Path, config: dict[str, str]) -> EvalResult:
        ...
```

In `pyproject.toml`:

```toml
[project.entry-points."novafabric.eval_suites"]
my-suite-v1 = "my_package.my_suite:MySuiteAdapter"
```

After `pip install -e .`, `nova eval list` will show the new suite. `nova eval run <capsule> --suite my-suite-v1` will invoke it.

The built-in suites (GAIA, SWE-bench, AgentBench, MMLU, Smoke) ship as OCI-pinned
containers and can gate promotion on regression via OPA/Rego — see the eval sections of
[`docs/cli-reference.md`](cli-reference.md).

## Writing a custom PII masker (ADR-0135) — experimental

Capture-time redaction is extensible: operator-registered **maskers** run at
capture **after** the built-in [ADR-0009](./decisions.md)
secret scanner and **before** the capsule is finalized. Built-ins always run and
can never be disabled by a plugin. Use a masker for imperative masking logic that
declarative regex packs cannot express — checksum-validated national IDs, internal
case numbers, format-preserving tokenizers.

A masker is a pure, deterministic, **offline** callable — no sockets, no capsule
writes; it returns a value, the pipeline writes and proves:

```python
# my_package/maskers.py
import re
from novafabric.masking import UNCHANGED, MaskContext, MaskField

class CaseIdMasker:
    masker_id = "acme-case-id"      # stable identity, attributed in the proof
    masker_version = "1"            # bump on behavior change
    pattern_ids = ("acme-case-number",)

    _RE = re.compile(r"ACME-CASE-\d+")

    def mask(self, field: MaskField, value: str, context: MaskContext) -> str | object:
        masked = self._RE.sub("[MASKED:acme-case-id]", value)
        return UNCHANGED if masked == value else masked   # decline with UNCHANGED
```

Register it either as a `novafabric.maskers` entry point:

```toml
[project.entry-points."novafabric.maskers"]
acme-case-id = "my_package.maskers:CaseIdMasker"
```

or by dotted import path in `.novafabric/masking.yaml` (auto-discovered by
`nova capture`; override with `--masking-config PATH`):

```yaml
masking:
  enabled: true
  maskers:
    - id: acme-case-id            # entry-point name or my_package.maskers:CaseIdMasker
      version: "1"
      timeout_ms: 50              # per-call budget; exceeding it fail-closes the field
      max_input_bytes: 65536      # per-call input cap
      on_error: redact            # redact (default) | drop — the fail-closed action
      config:                     # opaque, surfaced as context.masker_config
        prefix: "ACME-CASE"
```

Failure semantics (the contract your masker lives under):

- **Built-ins first, always.** Your masker observes already-redacted markers; it can
  never un-redact them.
- **Fail-closed on secrets.** If your masker raises, times out, exceeds the input cap,
  returns a non-string, or returns output still containing the raw value, the field is
  redacted (or dropped, per `on_error`) and the failure is recorded in the proof's
  `masker_errors[]`. The raw value is never written.
- **Fail-safe for the workload.** A masker failure never crashes or blocks the captured
  command; capture continues. A masker that cannot be *loaded*, however, aborts capture
  before the workload starts — expected masking is never silently skipped.
- **Every mask is evidence.** Each applied mask emits a `masker_findings[]` entry in
  `redaction-proof.json` (`masker_id`, `pattern_id`, `target_ref`, `match_hash` of the
  pre-mask bytes, `chain_position`); the proof stays hash-chained. An auditor can verify
  a candidate value with `hash(candidate) == match_hash` without the capsule ever
  holding the bytes.

Reference implementation: `novafabric.masking.examples.EmailMasker` (registered as
`novafabric-email`). See [ADR-0135](decisions.md) for the masking-pipeline decision; schemas:
`schemas/masking-config.schema.json`, `schemas/masker-finding.schema.json`,
`schemas/masker-error.schema.json`. ML-based PII detectors (e.g. Presidio) are **not**
bundled — an external masker package may wrap one, but it must stay offline and clear
the [ADR-0024](./decisions.md) license audit.

## Querying and extending PolicyStore

`novafabric.promote.policy_store.PolicyStore` stores signed promotion policy bundles in a SQLite DB. Key methods:

| Method | Description |
|---|---|
| `put(bundle_json, namespace)` | Insert a new policy bundle; returns version number |
| `get_latest(namespace)` | Return `bundle_json` for the highest-version policy |
| `get_by_version(version, namespace)` | Return `bundle_json` for a specific version |
| `get_active_at(timestamp, namespace)` | Return the policy active at a given UTC timestamp |
| `list_all(namespace=None)` | Return all rows as `list[dict]`; optional namespace filter |

The default DB path searched by `nova policy list` is `NOVAFABRIC_HOME/promote/policy.db`, falling back to `~/.local/share/novafabric/merkle.db` for backwards compatibility with older installations.

---

## Docs-honesty and maturity labels

When you touch any documentation surface, apply NovaFabric's four-label rule. Every
mention of a feature must be classifiable as exactly one of:

| Label | Meaning |
|---|---|
| **works today** | Implemented in main, tests pass, part of the shipped surface. |
| **experimental** | Implemented but unstable — the interface may change before the v1.0 schema freeze. |
| **planned** | On the roadmap with a target version; design intent captured in an ADR. |
| **future design** | Documented intent only, no implementation. |

Two hard rules that apply throughout this guide:

1. **Never claim a planned or experimental subsystem as a stable, shipped product
   feature in user-facing docs.** In-tree code can exist and have tests while the
   corresponding *product capability* is still experimental or planned — keep the two
   distinct. The scale-out subsystems in this guide (collector tier, topology
   dashboards, object capsule store) are engineering work toward planned architecture,
   not released features.
2. **NovaFabric produces evidence that *supports* compliance; it does not certify
   compliance.** It attests only that a capsule is unmodified since signing. Do not
   describe any exporter or profile as making a run "compliant" with the EU AI Act,
   NIST AI RMF, ISO/IEC 42001, GDPR, HIPAA, FDA 21 CFR Part 11, SOC 2, or any other
   framework.

The standard for this voice is the README's [when *not* to use NovaFabric](../README.md#when-to-use-novafabric)
section: state the limitation plainly, in the same sentence as the capability.

---

## Adding a database migration

Two independent Alembic tracks, selected by the config file:

```bash
alembic revision -m "add foo column"                             # SQLite / registry
alembic -c alembic-postgres.ini revision -m "add foo column"     # Postgres / server
```

| Track | Config | Versions |
|---|---|---|
| SQLite (registry) | `alembic.ini` | `alembic/sqlite/versions/` |
| Postgres | `alembic-postgres.ini` | `alembic/postgres/versions/` |

A change that affects both stores needs a revision in **both** trees; they do not
share a revision graph. The registry track is also shipped inside the wheel
(`src/novafabric/migrations/registry`) so an installed CLI can migrate without the
repository present — if you add a registry revision, confirm it is packaged.

**Always pass an ordinary libpq DSN.** `alembic/env.py` routes it through
`novafabric.metadata_store.dsn.to_sqlalchemy_url`, which rewrites a bare
`postgresql://` to `postgresql+psycopg://`:

```bash
NOVAFABRIC_POSTGRES_DSN=postgresql://user:pass@localhost:5432/nova \
  alembic -c alembic-postgres.ini upgrade head
```

SQLAlchemy resolves the bare scheme to **psycopg2**, which this project does not
ship — it depends on `psycopg[binary]` (psycopg 3). Without that normalisation the
command above dies with `ModuleNotFoundError: No module named 'psycopg2'`, which is
exactly what it did until 2026-08-05. **If you add another consumer of the Postgres
DSN, route it through that function** instead of re-deriving the rule; the bug
survived for weeks because each consumer was individually correct. Design rationale:
[architecture — one DSN, two consumers](architecture.md#one-dsn-two-consumers).

Verify against a real Postgres, not just by re-reading the revision:

```bash
docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=novafabric_test --name nf-pg postgres:16
NOVAFABRIC_POSTGRES_DSN=postgresql://postgres:test@localhost:55432/novafabric_test \
  uv run alembic -c alembic-postgres.ini upgrade head
NOVAFABRIC_TEST_POSTGRES_DSN=postgresql://postgres:test@localhost:55432/novafabric_test \
  uv run pytest tests/integration/
docker stop nf-pg
```

> Use a **throwaway `docker run --rm`** on a non-standard port as above. Do not use
> `docker compose down` on a shared host — it is project-wide and takes unrelated
> volumes with it.
>
> Run the integration suite against a **fresh** database. The migration is an
> idempotent upsert, so a second run into an already-populated database legitimately
> writes 0 rows — which looks exactly like silent data loss and is not.

---

## Publishing docs to the website

`docs/*.md` is rendered at `novafabric.ai/docs/` by `web/src/lib/docs.ts`, which reads
this directory **directly at build time** via `import.meta.glob`. Nothing is copied,
so the site cannot drift from the docs you edit — but it does mean a docs change is a
site change:

- Adding a file here adds a page and a sitemap entry automatically.
- The page title comes from the first `# heading`; the meta description from the first
  real prose paragraph. A file that opens with a badge row or a blockquote gets a
  worse description — lead with a sentence.
- Relative `.md` links are rewritten to site URLs; links that escape `docs/` go to
  GitHub, because no site route exists for them.
- `docs/releases/` and `docs/whitepaper/` are excluded.

Run `cd web && npm run build` after a structural docs change. It needs **Node ≥ 22.12**
(Astro 7).

---

## Where to go next

| If you want to… | Go to |
|---|---|
| Use the CLI (flags, commands, defaults) | [`docs/cli-reference.md`](cli-reference.md) |
| Understand the subsystem map and code structure | [`docs/architecture.md`](architecture.md) |
| Read the capture data-flow internals | [Capture hook mechanism](concepts.md#capture-hook-mechanism) |
| See release sequencing and roadmap | [`ROADMAP.md`](../ROADMAP.md) |
| Follow the contribution / RFC / commit rules | [`CONTRIBUTING.md`](../CONTRIBUTING.md) |
| Review a per-decision history | [`docs/decisions.md`](decisions.md) |
| Run the warm capture daemon as a user | [`docs/warm-capture-daemon.md`](warm-capture-daemon.md) |
| Add a schema migration | [Adding a database migration](#adding-a-database-migration) |
| Publish a doc page to the website | [Publishing docs to the website](#publishing-docs-to-the-website) |

Before opening a PR, re-run the four [quality gates](#quality-gates) (pytest ≥ 90%
coverage, ruff, mypy, link check) and, for any CLI change, smoke-test
`uv run nova --help` and the affected sub-command.
