# Contributing to NovaFabric

Thank you for your interest in contributing.

NovaFabric is a foundation-ready open-source project. Before opening a
non-trivial PR, please read [`GOVERNANCE.md`](GOVERNANCE.md) and the
[RFC process](design/governance/RFC-0000-rfc-process.md). For smaller
contributions (bug fixes, doc improvements, new tests), the process below is
all you need.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package manager

## Setup

```bash
git clone git@github.com:novafabric/novafabric.git
cd novafabric
uv sync --dev
```

## Running tests

```bash
uv run pytest --benchmark-disable --cov=novafabric --cov-report=term-missing
```

Coverage must remain at or above 90%.

The `--benchmark-disable` flag skips the 100-round NovaSeal latency benchmark so
normal test runs complete quickly.  To run the benchmark and enforce the p99 gate:

```bash
uv run pytest tests/seal/test_benchmark.py -v
# or via Make:
make benchmark
```

The gate asserts `NovaSeal.seal()` p99 < 200 ms over 100 rounds.  This runs as a
separate `seal-latency-gate` job in CI on every PR.

## Linting

```bash
uv run ruff check src tests
```

## Type checking

```bash
uv run mypy src
```

All three must pass before submitting a PR.

## Dashboard bundle

The static site served by `nova serve --experimental` lives in
`src/novafabric/serve/static/` and is **not tracked by git**. After any change
to `web/src/` you must rebuild it before tagging a release:

```bash
make bundle
# or, from web/:
npm run build:dashboard
```

Failing to do this means `nova serve` users will run stale UI even after the
source is correct.

## Pull requests

- Open an issue first for non-trivial changes.
- For changes that affect a public schema, the CLI surface, dependencies,
  storage, or security posture, an [RFC](design/governance/RFC-0000-rfc-process.md)
  is required.
- Keep PRs focused — one feature or fix per PR.
- Update `CHANGELOG.md` under `## Unreleased` for user-facing changes.
- Rebuild the dashboard bundle (`make bundle`) if `web/src/` changed.
- Ensure all quality gates pass locally before pushing.

## Commit style

Use lowercase imperative: `feat: add dataset asset type`, `fix: catch missing file in validator`.

## When to write an RFC vs. open a PR

| Scenario | Channel |
|---|---|
| Typo, link fix, doc improvement | PR |
| Bug fix with test | PR |
| Refactor that preserves behavior | PR |
| New optional adapter | PR (with maintainer review) |
| New CLI command or default flag | RFC |
| New runtime dependency (Tier A: Apache-2.0/MIT/BSD/PostgreSQL License) | PR with one-line license note |
| New runtime dependency (Tier B: LGPL/MPL-2.0/EPL-2.0) | RFC + filled [evaluation template](design/templates/database-evaluation-template.md) |
| New runtime dependency (Tier C: AGPL/SSPL/BSL/GPL/Elastic) | ADR with business justification + migration path |
| Schema change (Run Capsule, Asset Spec, Evidence Bundle) | RFC |
| Storage format change | RFC |
| Security-relevant change | RFC + threat model update |
| Governance change | RFC |

The RFC process is described in
[`design/governance/RFC-0000-rfc-process.md`](design/governance/RFC-0000-rfc-process.md).

## Becoming a maintainer

The path is documented in
[`design/governance/MAINTAINER_CRITERIA.md`](design/governance/MAINTAINER_CRITERIA.md).
It is intentionally informal: sustained, high-quality contribution is what
counts.

## Code of conduct

Participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Security

Vulnerability reports go to the address in [`SECURITY.md`](SECURITY.md), not
to a public issue. The threat model that informs our security posture is
[`THREAT_MODEL.md`](THREAT_MODEL.md).

## Design Partners

If your organization wants to adopt NovaFabric in production before v1.0
in exchange for input on the spec direction, see
[`design/governance/DESIGN_PARTNERS.md`](design/governance/DESIGN_PARTNERS.md).
