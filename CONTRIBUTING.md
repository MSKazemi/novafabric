# Contributing to NovaFabric

**Contributions are genuinely wanted here, and small ones are wanted most.** A
typo fix, a clearer error message, a test for an edge case you hit — those are
real contributions and they get reviewed like real contributions.

New to the project? Start with **[your first contribution](#your-first-contribution)**
below. It takes about 15 minutes end to end, and you do not need to read
anything else on this page first.

---

## Your first contribution

### 1. Find something to work on — 2 minutes

- **[Good first issues](https://github.com/MSKazemi/novafabric/labels/good%20first%20issue)** —
  scoped, with the file paths and the definition of done written out
- **[Help wanted](https://github.com/MSKazemi/novafabric/labels/help%20wanted)** —
  larger, still well-specified
- **Something that annoyed you** — a confusing error, a doc that lied, a missing
  flag. You do not need permission to fix that; open a PR.
- **[Now / Next / Later](ROADMAP.md#now--next--later--the-10-second-version)** — if
  you would rather see where the project is heading before picking something up.

If nothing fits, say hello in
[Discussions](https://github.com/MSKazemi/novafabric/discussions) and we will
find you something.

### 2. Set up — 5 minutes

```bash
git clone git@github.com:MSKazemi/novafabric.git
cd novafabric
uv sync --all-extras
```

The only prerequisite is [uv](https://docs.astral.sh/uv/).

> **Use `--all-extras`.** A plain `uv sync` strips the optional extras
> (sigstore/nats/clickhouse/psycopg…) from the venv, and the ~30 tests that
> exercise them fail with import errors that look like your fault but are not.

Prefer a one-click environment? The repo ships a
[devcontainer](.devcontainer/devcontainer.json) — open it in GitHub Codespaces or
VS Code and the setup above runs for you.

### 3. Make the change and prove it works — 5 minutes

```bash
uv run pytest tests/<the-area-you-touched>   # fast, targeted
make test-fast                               # the whole fast tier (~90 s)
```

Write the test first if you can. If you are fixing a bug, a test that fails
before your fix and passes after it is the most persuasive thing you can put in a
PR.

### 4. Run the gates and open the PR — 3 minutes

```bash
make test-fast     # tests
make lint          # ruff
make typecheck     # mypy
make check-links   # docs links resolve
```

Then push and open a pull request. Use a lowercase imperative title:
`fix: catch missing file in validator`.

**That's it.** You do not need to read the RFC process, the governance document,
or the license tiers for a change like this. They exist for bigger changes and
they are linked below when you need them.

---

## What happens next

We commit to this, and you can hold us to it:

| Event | Our commitment |
|---|---|
| You open an issue | First response within **3 business days** |
| You open a PR | First review within **5 business days** |
| You ask in Discussions | Response within **1 week** |
| Your PR is approved | Merged within 2 business days |

If we miss one of those, ping the thread — that is not rude, it is the system
working. See [SUPPORT.md](SUPPORT.md) for the full set.

Every contributor is credited in [CONTRIBUTORS.md](CONTRIBUTORS.md). Sustained
contribution leads to steward and then maintainer status; the path is written
down in [maintainer criteria](docs/governance/maintainer-criteria.md), and
self-nomination is welcome.

---

## The full development reference

Everything below is reference material. You do not need it for a first
contribution.

### Running tests

The suite is ~11.5K tests — use the tiered targets instead of a serial full run:

```bash
make test-fast   # dev loop: parallel (-n auto), no coverage, skips integration
                 # + the testcontainers Postgres tier (~90 s)
make test-par    # release gate — exactly what CI's `unit` job runs (~5 min)
make test        # everything, serial, incl. tests/integration — WIDER than the
                 # gate above, and without its coverage floor
```

Coverage must remain at or above 90%.

A suite-wide `pytest-timeout` (300 s per test) makes hangs fail by name, and an
autouse fixture strips ambient `NOVAFABRIC_*` env vars so tests never read or
write your real registry or capsule store.

The `--benchmark-disable` flag skips the 100-round NovaSeal latency benchmark so
normal runs stay quick. To run it and enforce the p99 gate:

```bash
make benchmark   # asserts NovaSeal.seal() p99 < 200 ms over 100 rounds
```

This also runs as a separate `seal-latency-gate` job in CI on every PR.

### The quality gates

```bash
make lint          # ruff check src tests scripts
make typecheck     # mypy src
make check-links   # every relative link in a public doc resolves
```

All must pass before a PR is merged. For a CLI change, also smoke-test
`uv run nova --help` and the affected sub-command.

### Dashboard bundle

The static site served by `nova serve --experimental` lives in
`src/novafabric/serve/static/` and is **not tracked by git**. After any change to
`web/src/` you must rebuild it before tagging a release:

```bash
make bundle
# or, from web/:
npm run build:dashboard
```

Skipping this means `nova serve` users run stale UI even when the source is
correct.

### Pull request expectations

- Keep PRs focused — one feature or fix per PR
- Update `CHANGELOG.md` under `## Unreleased` for user-facing changes
- Rebuild the dashboard bundle (`make bundle`) if `web/src/` changed
- Ensure the gates pass locally before pushing
- For non-trivial changes, open an issue or Discussion first — it saves you from
  building something that was going to be declined

### Commit style

Lowercase imperative: `feat: add dataset asset type`,
`fix: catch missing file in validator`.

---

## Documentation status labels

Every doc that mentions a feature must label it as exactly one of these. **Never
blur the line, and never claim a planned feature as implemented** — an
overclaiming doc costs a user hours and costs the project trust it cannot buy
back.

| Label | Means |
|---|---|
| **works today** | Implemented on `main`, tests pass |
| **experimental** | Implemented, interface may still change |
| **planned** | Roadmapped, has a target version, not yet built |
| **future design** | Documented intent, no implementation |

The standard for the voice is the README's
[when *not* to use NovaFabric](README.md#when-to-use-novafabric) section: state
the limitation in the same breath as the capability.

---

## When to write an RFC instead of a PR

| Scenario | Channel |
|---|---|
| Typo, link fix, doc improvement | PR |
| Bug fix with test | PR |
| Refactor that preserves behavior | PR |
| New optional adapter | PR (with maintainer review) |
| New CLI command or default flag | RFC |
| Schema change (Run Capsule, Asset Spec, Evidence Bundle) | RFC |
| Storage format change | RFC |
| Security-relevant change | RFC + threat-model update |
| Governance change | RFC |
| New runtime dependency (Tier A: Apache-2.0/MIT/BSD/PostgreSQL) | PR with a one-line license note |
| New runtime dependency (Tier B: LGPL/MPL-2.0/EPL-2.0) | RFC + a `[[declaration]]` in `.license-policy.toml` |
| New runtime dependency (Tier C: AGPL/SSPL/BSL/GPL/Elastic) | ADR with justification + migration path, then a `[[declaration]]` carrying both |
| New runtime dependency (Tier D: field-of-use / "ethical source" terms) | Not accepted — no waiver exists |

Unsure? [Ask in Discussions](https://github.com/MSKazemi/novafabric/discussions).
A maintainer will tell you which channel applies. Asking is always cheaper than
writing the wrong document.

The process itself: [docs/governance/rfc-process.md](docs/governance/rfc-process.md).
Public RFCs live in [`docs/rfcs/`](docs/rfcs/); accepted architectural decisions
are indexed in [`docs/decisions.md`](docs/decisions.md).

**The license tiers are enforced, not just documented.**
`scripts/license_gate.py` runs in CI on every push and PR, plus weekly so an
upstream *relicense* cannot slip through an unchanged lockfile. Run it locally
before opening a PR that adds a dependency:

```bash
uv run python scripts/license_gate.py --ignore novafabric          # gate
uv run python scripts/license_gate.py --ignore novafabric --list   # full inventory
```

---

## AI assistance

**AI assistance is welcome**, and this project uses it. The commit history records a
single author throughout: a tool is a tool, and whoever submits a change owns it. It
would be incoherent to accept AI assistance for maintainers and hold contributors to a
different standard.

The bar is about responsibility, not tooling:

> However a change was produced, **you must understand it, be able to explain it in
> review, have run the gates locally, and take responsibility for it.** Please
> disclose substantial AI assistance in the pull-request description.

Disclosure is not a warning label. It tells a reviewer where to look first, the same
way "this is my first time in this subsystem" would.

**What we will never do** is reject a change *because* it was AI-assisted. If a pull
request is turned down it will be for a stated defect — untested, doesn't match the
design, or the author cannot explain why this approach over another. That last question
is the whole filter, and it is one any author of their own work answers easily.

Two things make this workable rather than aspirational: CI runs the full gates, so
unverified code fails before a human reads it; and the PR template asks you to confirm
you ran them.

If you are a coding agent, [`AGENTS.md`](AGENTS.md) is written for you — architecture,
the exact commands, the invariants that get a change reverted, and what to do when a
test fails that you cannot fix.

---

## Licensing your contribution

**NovaFabric is Apache-2.0 and stays that way.** Nothing here changes the license you receive
the code under, and nothing here asks you to give up ownership of your work.

**What it takes:** one comment on your first pull request. A bot posts a link to the
[CLA](CLA.md), you reply once, and it covers everything you contribute afterwards. Nothing to
email, no account to create, no form.

**What you keep — all of it.** The copyright in your contribution stays yours. You are granting
a license, not transferring ownership, and your own code remains yours to use anywhere else,
including in other projects. Your contribution ships under Apache-2.0 like the rest of the
project, and every release stays Apache-2.0.

**Why it exists.** Copyright belongs to whoever wrote the code. That is normally fine — until
the project needs to make a licensing decision, at which point it needs permission from every
person who ever contributed. Projects that skip this step find out years later that they cannot
adopt a newer license version, or respond to a license change in a dependency, because a
contributor from 2027 is unreachable. This keeps those doors open.

If you cannot agree to part of it, [open a Discussion](https://github.com/MSKazemi/novafabric/discussions)
— we would rather adapt the agreement than lose your work.

---

## Code of conduct

Participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). It is
enforced, including for maintainers.

## Security

Vulnerability reports go through the process in [`SECURITY.md`](SECURITY.md) —
private disclosure, never a public issue.

## Design partners

If your organization wants to adopt NovaFabric in production before v1.0 in
exchange for real influence over the spec, see
[the design partner program](docs/governance/design-partners.md). Three
independent design-partner sign-offs are the last gate on freezing the v1.0
format, and there are currently zero.

## Governance

How decisions get made, who decides, and how that changes over time:
[`GOVERNANCE.md`](GOVERNANCE.md).
