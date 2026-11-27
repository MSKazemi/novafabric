# CI workflows: matrix and audit

This page lists every GitHub Actions workflow in `.github/workflows/`: what starts it, what its jobs
check, what it gates, and how it is configured. It also lists what the audit for issue #217 found.
It describes the tree as of 2026-11-27 and the run history up to that date. When you add or change a
workflow, update its row here.

Related pages: [Release process](release-process.md) (how a tag becomes a release),
[Developer guide](developer-guide.md) ("How CI runs the suite": the six-shard `unit` job), and the
[release verdict design](release-verdict-design.md) (a proposal, not active).

## What actually gates what (works today)

- **Nothing blocks a merge to `main` automatically.** `main` has no classic branch protection. Its
  one ruleset, "main: no force-push or deletion", contains only the `non_fast_forward` and
  `deletion` rules. No status check is required (GitHub API, `repos/MSKazemi/novafabric/rulesets`,
  read 2026-11-27). Every per-PR check below is therefore advisory: a reviewer has to read it.
- **A release is blocked by exactly one workflow: `ci.yml`.** Every tag-triggered workflow
  (`release.yml`, `publish-pypi.yml`, `publish-image.yml`, `publish-chart.yml`) first calls
  `require-green-ci.yml`. That job waits for the `ci.yml` run on the tagged commit and fails unless
  the run concluded `success`. No other workflow is consulted. `scripts/require_green_ci.py`
  carries a wider release manifest (`RELEASE_POLICY_V1`, covering Collector CI, the audits and the
  metadata-store gate), but `main()` and the workflows do not call it. It is **future design**; see
  [release verdict design](release-verdict-design.md).
- **Release tags `v*` are immutable.** The "release tags are immutable" ruleset blocks
  force-pushing, deleting or updating them.
- **The workflow token defaults to read-only.** The repository default is
  `default_workflow_permissions: read`. Every workflow also declares its own `permissions:`.
- **Every job runs only in the public repository** (`if: github.repository ==
  'MSKazemi/novafabric'`, enforced by `tests/docs/test_workflows_run_only_on_the_public_repo.py`).
  It runs on the pinned `ubuntu-24.04` image, never `ubuntu-latest`
  (`tests/docs/test_workflow_runner_images_are_pinned.py`).

## Workflow matrix

Abbreviations: **PR** = `pull_request` to `main` unless noted, **push** = push to `main`,
**sched** = `schedule`, **manual** = `workflow_dispatch`, *(paths)* = only when listed paths change.
**Pinned** means every third-party `uses:` names a full 40-hex commit SHA with a `# vX.Y.Z` comment.
Every workflow passes that check today. **Runner** is `ubuntu-24.04` for every job. The timeouts
listed are the values after this change.

| Workflow | Trigger | Jobs | What it gates | Timeout (min) | Permissions | Pinned | Required |
|---|---|---|---|---|---|---|---|
| `ci.yml` | PR, push, manual | `lint`, `unit` ×6 shards, `coverage`, `typecheck`, `golden-capsule`, `seal-latency-gate`, `capture-overhead-gate`, `web`, `integration` | Lockfile sync, ruff, CLI smoke, the whole pytest suite (90% coverage floor, shard partition), mypy strict, wheel-installed EvidenceLab and replay oracles, seal median latency, capture p95, Astro site build and vitest, Postgres integration tier | 10 / 30 / 15 / 15 / 20 / 5 / 10 / 10 / 20 | `contents: read` | yes | **release gate** (via `require-green-ci.yml`); not a merge check |
| `collector-ci.yml` | PR *(paths)*, push *(paths)* | `Lint`, `Vulnerability Scan`, `Unit Tests`, `Spec Corpus Validation`, `Build Artifacts`, `Security Scan` | Go collector: vet and a spool lock-primitive ban, govulncheck (blocking), `go test -race`, event-envelope corpus fields, binary build, licence ban list | 10 / 10 / 15 / 5 / 10 / 15 (**added**) | `contents: read` | yes | no |
| `metadata_store_security_gate.yml` | PR *(paths)*, push *(paths)*, manual | `MetadataStore Security Gate` | ruff, mypy on `metadata_store/`, two RLS grep invariants, metadata-store tests (85% floor), SET LOCAL kill proof | 20 (**added**) | `contents: read` | yes | no |
| `mcp-conformance.yml` | PR *(paths)*, push *(paths)* | `conformance` | MCP conformance vectors, Server Card validation, MCP proxy tests | 10 | `contents: read` | yes | no |
| `dashboard.yml` | PR *(paths)*, push *(paths)* | `dashboard` | `packages/nova-dashboard`: type check, unit tests, build | 10 | `contents: read` | yes | no |
| `sdk-ts.yml` | PR *(paths)*, push *(paths)* | `sdk-ts` | TypeScript SDK: tsc, vitest, OpenAPI type drift, ESM/CJS consumption | 10 | `contents: read` | yes | no |
| `docs.yml` | PR, push, manual | `links` | Every relative link in a public doc resolves; decisions index is current | 5 | `contents: read` | yes | no |
| `capture-action.yml` | PR, push, sched (Mon), manual | `success-path`, `failure-path`, `artifact-survives-failure` | The repository's composite capture action: a passing and a crashing workload are both captured, and the capsule survives a failed step | 10 / 10 / 5 | `contents: read` | yes | no |
| `pip-audit.yml` | PR, push, sched (Tue), manual | `audit` | Locked Python dependencies: HIGH/CRITICAL advisories, with expiring waivers | 15 | `contents: read` | yes | no |
| `npm-audit.yml` | PR *(paths)*, push *(paths)*, sched (Tue), manual | `audit` ×3 trees | The three npm lockfiles: severity and waiver gate | 10 | `contents: read` | yes | no |
| `license-policy.yml` | PR, push, sched (Tue), manual | `license-policy` | Python dependency licences against the Tier A/B/C policy | 15 | `contents: read` | yes | no |
| `nightly-scale-gates.yml` | sched (daily 03:00 UTC), manual | `postgres-scale`, `object-store-scale`, `dashboard-scale` | Postgres and testcontainers tiers, S3-compatible object-store concurrency and WORM tier (SeaweedFS), 100K-row dashboard p95 | 40 / 45 / 20 | `contents: read` | yes | no |
| `scorecard.yml` | push, sched (Mon), `branch_protection_rule` | `Scorecard analysis` | Nothing: an OpenSSF Scorecard report, uploaded as SARIF to code scanning | 15 (**added**) | workflow: `contents: read` (**was `read-all`**); job: `security-events: write`, `id-token: write`, `contents: read`, `actions: read` | yes | no |
| `cla.yml` | `pull_request_target`, `issue_comment` | `cla` | CLA signature status on human pull requests | 5 (**added**) | workflow: `{}`; job: `actions`, `contents`, `pull-requests`, `statuses` write (what the CLA action needs) | yes | no |
| `release-toolchain.yml` | PR *(paths)*, push *(paths)*, manual | `image-toolchain`, `chart-toolchain` | Rehearses the image build/sign toolchain and the Helm chart lint/package without publishing | 30 / 10 | `contents: read` | yes | no |
| `require-green-ci.yml` | `workflow_call` | `ci-green` | Waits for `ci.yml` on the tagged SHA and fails unless it succeeded | 110 | `actions: read`, `contents: read` | yes | called by every release workflow |
| `release.yml` | tag `v*` | `ci-green` → `github-release` | Creates the GitHub Release from `docs/releases/<tag>.md` | 5 | workflow `contents: read`; job `contents: write` | yes | release |
| `publish-pypi.yml` | tag `v*`, manual | `ci-green` → `build` → `publish` | Build, twine check, SBOM, SLSA provenance, PyPI trusted publishing (`pypi` environment) | 10 / 10 | workflow `contents: read`; jobs `id-token: write`, `attestations: write` | yes | release |
| `publish-image.yml` | tag `v*`, manual | `ci-green` → `image` | Multi-arch image to GHCR (and Docker Hub when a token is set), cosign signature, provenance, trivy CRITICAL gate | 30 | workflow `contents: read`; job `packages`, `id-token`, `attestations` write | yes | release |
| `publish-chart.yml` | tag `v*`, manual | `ci-green` → `chart` | Helm lint, package, push to GHCR OCI | 10 | workflow `contents: read`; job `packages: write` | yes | release |

The `ci-green` jobs in the four release workflows are reusable-workflow calls. GitHub does not allow
`timeout-minutes` on such a call; the 110-minute cap lives in the called job.

**Toolchain versions.** Go is pinned exactly (`GO_VERSION: "1.26.11"` in `collector-ci.yml`) and so
is Helm (`v3.16.4`). Python is pinned to a minor version (`"3.12"`): the patch level floats with the
runner's tool cache (3.12.16 on 2026-11-27). Node is `"22"` with `check-latest: true`, so it floats
on purpose, to pick up security releases. `uv` is not pinned: `setup-uv` installs the newest release
(0.13.10 on 2026-11-27). Python dependencies come from `uv.lock`. Most jobs install with
`uv sync --frozen`, and `lint` checks with `uv lock --check` that the lock matches `pyproject.toml`.

## Code scanning and secret scanning (works today)

Read from the GitHub API on 2026-11-27:

- **Secret scanning: on. Push protection: on.** Non-provider patterns and validity checks are off.
- **CodeQL: not configured.** The default setup reports `state: not-configured`, and no workflow runs
  CodeQL. The only SARIF reaching code scanning comes from Scorecard.
- **No other SAST runs.** ruff selects `E`, `F`, `I` and `C4` only. The bandit-derived `S` rules
  are deferred in `pyproject.toml`, with 239 sites to review. Nothing scans Go code for
  security patterns beyond govulncheck, which covers known-vulnerable dependencies.
  Nothing lints the workflows themselves.

## Findings

### Fixed in this change (non-release workflows only)

1. **Nine jobs had no `timeout-minutes`**, so a hung job held a runner for GitHub's 360-minute
   default. These were the six `collector-ci.yml` jobs, `metadata_store_security_gate.yml`, `cla.yml`
   and `scorecard.yml`. Each now has a cap. The cap is sized from the slowest of its last 8 to 15
   green runs, with several times that as headroom; the measured value is in a comment beside each
   cap.
2. **`scorecard.yml` granted `permissions: read-all` at workflow level.** It now grants
   `contents: read`, like every other workflow. The job's own permissions are unchanged, and they
   replace the workflow-level ones, so the job keeps exactly the token it had. scorecard-action
   forbids only workflow-level *write* permissions when `publish_results` is on.
3. **A pull request that edited `collector-ci.yml` did not run it.** The `push` path filter listed
   the workflow file and the `pull_request` filter did not. Both list it now.
4. `tests/docs/test_ci_workflow_hardening.py` guards items 1 and 2 for these four files. It checks
   that every job has a bounded timeout, that every third-party action is pinned to a SHA with a
   version comment, and that the workflow-level permissions are read-only. Its scope is
   deliberately these four files. The other workflows passed the same checks when audited, and can
   be added to its `HARDENED` list.

### The OpenSSF Scorecard job is not failing chronically

Over the last 200 runs (up to 2026-11-27), Scorecard concluded `success` 189 times and `failure`
11 times. The failures have two upstream causes, and neither is a defect in this repository:

- **10 failures in a row on 2026-11-24, 11:06–18:47 UTC.** The `docker pull
  ghcr.io/ossf/scorecard-action:v2.6.0` step failed three times with `manifest unknown` each run.
  The same action SHA (`23f22cd…`, v2.6.0) pulled that image successfully at 10:18 and again at
  21:35, and every run since has passed. So the image tag was briefly unresolvable on GHCR.
  scorecard-action's `action.yaml` names its image by tag
  (`docker://ghcr.io/ossf/scorecard-action:v2.6.0`), not by digest. Pinning the action to a SHA
  therefore does not pin the image it runs, and a registry-side problem with that tag fails the job.
- **1 failure on 2026-10-22.** Scorecard exited with `internal error: ListCommits … githubv4.Query:
  Something went wrong while executing your query`. That was a transient GitHub GraphQL error.

Scorecard is informational: nothing depends on it, and no release waits for it. A future outage of
the same kind would also be red without meaning anything about this code. The 15-minute cap added
here bounds the action's pull-retry loop.

### Open findings: non-release workflows (not changed here)

These change what a job checks, how it installs, or a file another open pull request is editing,
so they are listed for review instead of fixed.

1. **Steps that can never fail**, all in `collector-ci.yml`:
   - The `Security Scan` govulncheck step ends `|| echo "Vulnerabilities found — review required"`.
     It duplicates the blocking `Vulnerability Scan` job (same tool, same version) and can never fail.
   - In the same job, `go-licenses report … || true` lets a failed report produce an empty file. The
     ban-list grep then finds nothing and the step passes.
   - In `Build Artifacts`, the `--help` smoke tests end `| head -3 || true`, so they can never fail.
   - In `Unit Tests`, the 75% coverage check only prints a warning.
2. **A step that is a no-op in this repository.** `metadata_store_security_gate.yml` "Benchmark
   smoke" runs a script under `bench/`, which is not part of the public tree. It always prints a
   skip notice.
3. **Duplicated work.**
   - `metadata_store_security_gate.yml` re-runs `ruff check src/ tests/ scripts/`, the same command
     as `ci.yml` `lint`. It also runs `tests/metadata_store/`, which the `ci.yml` unit shards and the
     nightly `postgres-scale` job run too.
   - `mcp-conformance.yml` re-runs `tests/mcp_conformance` and the MCP proxy tests, which the unit
     shards run as well. Its CLI vector replay and Server Card validation are unique to it.
   - `collector-ci.yml` runs govulncheck twice (item 1).
4. **Unfrozen installs.** `metadata_store_security_gate.yml`, `license-policy.yml` and
   `mcp-conformance.yml` run `uv sync --all-extras` without `--frozen`. If `uv.lock` is stale, uv
   then re-locks on the runner and installs the new resolution instead of failing. `ci.yml` `lint` catches a stale lock on the same commit,
   so in practice this matters only for these jobs' own reproducibility. `mcp-conformance.yml` also
   sets no Python version, so uv picks the runner's interpreter.
5. **`uv` itself is unpinned** in every `setup-uv` step (see Toolchain versions above).
6. **No `concurrency:` group** in `collector-ci.yml`, `metadata_store_security_gate.yml`,
   `mcp-conformance.yml`, `nightly-scale-gates.yml`, `scorecard.yml` or `cla.yml`. Superseded
   pull-request runs of these keep running.
7. **Path-filtered workflows cannot simply be made required checks.** These are `collector-ci.yml`,
   `metadata_store_security_gate.yml`, `mcp-conformance.yml`, `dashboard.yml`, `sdk-ts.yml`,
   `npm-audit.yml` and `release-toolchain.yml`. A workflow that is skipped by its path filter never
   reports, so a required check on it would leave unrelated pull requests pending. This bears on
   any future merge rules.
8. **Dependabot does not watch the composite action.** The `github-actions` entry in
   `.github/dependabot.yml` has `directory: "/"`. That covers `.github/workflows/` and a root
   `action.yml`, but not `.github/actions/capture/action.yml`. The two have already drifted: the
   composite action pins `astral-sh/setup-uv` v10.3.2 and the workflows pin v10.4.0. The fix is to
   add `/.github/actions/*` to that entry. Two open pull requests edit `.github/dependabot.yml`, so it
   is not changed here.
9. **No SAST and no workflow linting** (see the section above). Enabling CodeQL default setup is a
   repository setting, not a file change, and costs no Actions minutes on a public repository.
   Whether to turn it on is an owner decision.
10. **The repository does not require SHA-pinned actions.** The setting is
    `sha_pinning_required: false` with `allowed_actions: all`. Every workflow pins today, but
    nothing outside the tests enforces it.
11. **CI failures alert no one beyond GitHub's defaults.** A red scheduled run (the nightly gates,
    the weekly audits, Scorecard) produces no issue and no notification other than GitHub's own email
    to the person who last edited the schedule. The nightly `postgres-scale` job was red on 11
    consecutive nights (2026-11-12 to 2026-11-22). The cause was `pg_restore` 17 on the
    `ubuntu-latest` image sending `SET transaction_timeout` to a Postgres 16 server. It was fixed by
    pinning the runner (#207).

### Open findings: release and publish workflows (for the owner; not edited)

`release.yml`, `publish-pypi.yml`, `publish-image.yml`, `publish-chart.yml` and the reusable
`require-green-ci.yml` were reviewed and left unchanged.

1. **The release gate checks `ci.yml` only.** Collector CI, the audits and the metadata-store gate
   can be red on a tagged commit, and that commit still publishes. The design for a wider gate is in
   [release verdict design](release-verdict-design.md) (not active).
2. **The image is pushed and signed before it is scanned.** In `publish-image.yml`, the trivy
   CRITICAL gate runs after `Build and push` and after `cosign sign`. A failing scan therefore leaves
   a signed image, and its version tags, already in the registry. An open draft pull request
   proposes scanning a candidate before any production tag.
3. **Manual dispatch can publish.** `publish-pypi.yml`, `publish-image.yml` and `publish-chart.yml`
   all accept `workflow_dispatch`. For the image, a dispatch from a branch pushes branch and short-SHA
   tags. The green-CI gate still applies to the dispatched SHA.
4. **Only PyPI uses a deployment environment** (`environment: pypi`). The image and chart jobs hold
   `packages: write` with no environment and so no required reviewer.
5. **`publish-chart.yml` puts a secret straight into a shell line**:
   `echo "${{ secrets.GITHUB_TOKEN }}" | helm registry login …`. Passing it through `env:` is the
   documented safe pattern. The chart is also pushed without a signature or provenance, unlike the
   image and the PyPI distributions.
6. **The release workflows have no `concurrency:` group.** Two tags pushed close together run their
   publish jobs in parallel.
7. **The PyPI SBOM step is `continue-on-error: true`**, and the full trivy report runs with
   `exit-code: "0"`. Both are deliberate and documented inline: an SBOM failure once blocked five
   releases from PyPI, and the separate CRITICAL step is the trivy gate. They are recorded here so
   the matrix is complete.

## Guards that keep this true

| Guard | What it fails on |
|---|---|
| `tests/docs/test_workflows_run_only_on_the_public_repo.py` | A job without the public-repository `if:` guard |
| `tests/docs/test_workflow_runner_images_are_pinned.py` | A `-latest` runner label; a Postgres service whose major differs from the runner's client tools |
| `tests/docs/test_publishing_jobs_are_repo_guarded.py` | A publishing job that could run outside the public repository |
| `tests/docs/test_release_workflow_is_idempotent.py` | `release.yml` failing when the release already exists |
| `tests/docs/test_release_toolchain_matches_publish.py` | The rehearsal drifting from the publish workflows |
| `tests/test_require_green_ci.py` | The release gate passing anything other than a successful `ci.yml` run |
| `tests/docs/test_ci_workflow_hardening.py` | A missing or unbounded timeout, an unpinned action, or broad workflow permissions in the four workflows hardened by #217 |
