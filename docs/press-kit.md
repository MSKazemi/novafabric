# Press & media kit

Everything you need to write about, present, or reference NovaFabric — without
having to ask us first, and without having to guess at the facts.

**You do not need permission** to write about NovaFabric, review it, benchmark it,
give a talk about it, or use the logo when referring to the project. The assets on
this page are published for exactly that purpose. The one thing we ask: **do not
imply endorsement** of your product or company by the NovaFabric project.

If something here is out of date or you need an asset that is missing,
[open an issue](https://github.com/MSKazemi/novafabric/issues/new/choose) — we treat
that as a real bug.

---

## The facts, at a glance

| | |
|---|---|
| **Name** | NovaFabric — one word, capital N, capital F. Never "Nova Fabric", "Novafabric", or "NOVAFABRIC". |
| **What it is** | An open-source, self-hosted execution-capsule system for AI and HPC workloads |
| **License** | Apache-2.0 |
| **Language** | Python 3.12+ |
| **Current version** | v0.101.0 — **beta**; on-disk formats are not frozen until the v1.0 schema freeze |
| **Repository** | <https://github.com/MSKazemi/novafabric> |
| **Website** | <https://novafabric.ai> |
| **Package** | `pip install novafabric` — <https://pypi.org/project/novafabric/> |
| **Created and maintained by** | [Mohsen Seyedkazemi Ardebili](https://github.com/MSKazemi) — AI systems engineer, platform architect, HPC researcher |
| **Governance** | [GOVERNANCE.md](../GOVERNANCE.md) · [MAINTAINERS.md](../MAINTAINERS.md) |
| **Telemetry** | None. No accounts, no phone-home, no update checks. |

---

## Boilerplate descriptions

Copy these verbatim. They are written to be accurate, not promotional.

### One line (≤ 100 characters)

> NovaFabric turns any command into a portable, replayable, signed evidence capsule.

### Short (≈ 40 words)

> NovaFabric is an open-source, self-hosted tool that captures any command — a
> script, an AI agent, a model run, an HPC job — as a portable, secret-redacted
> evidence capsule you can replay, diff, and cryptographically verify months later.

### Standard (≈ 90 words)

> NovaFabric is an open-source, self-hosted execution-capsule system for AI and HPC
> workloads. It captures any command with no application code changes and produces a
> schema-valid, secret-redacted folder — the capsule — containing the manifest,
> traces, model and tool calls, an environment lock, a redaction proof, and a replay
> policy. Captured runs can be replayed in four modes, structurally diffed against
> each other, linked into a lineage graph, and signed with in-toto DSSE, Sigstore, or
> RFC 3161 timestamps. It runs entirely in your own infrastructure, from a laptop to
> a cluster, online or air-gapped. Apache-2.0.

### The positioning sentence, if you need to explain *why* it exists

> Tracing tells you what happened. NovaFabric tells you whether a past run can be
> replayed, compared, and proven.

---

## What NovaFabric is **not**

Please include this if you are writing a comparison — getting it wrong is the single
most common error in coverage of this project, and an inaccurate comparison costs
readers days.

- **Not a live monitoring or alerting platform.** If you want dashboards and pages at
  3 a.m., use an observability tool. NovaFabric answers questions about the past.
- **Not a hosted service.** There is no SaaS tier, no account, and nothing to sign up
  for. Self-hosted is the only mode.
- **Not a claim of exact replay for remote LLM calls.** A remote model is not
  deterministic and NovaFabric does not pretend otherwise; that is what the `mocked`,
  `semantic`, and `forensic` replay modes exist for.
- **Not a compliance certification.** NovaFabric produces conformance *receipts* and
  evidence, never verdicts. See [assurance cases](assurance-cases.md).

The full, honest comparison — **including where NovaFabric loses** — is in
[How NovaFabric compares](comparison.md).

---

## Logo and brand assets

| Asset | File | Use for |
|---|---|---|
| Mark (square badge) | [`novafabric-mark.svg`](assets/brand/novafabric-mark.svg) | Avatars, favicons, slide corners, anywhere small |
| Wordmark (mark + name) | [`novafabric-wordmark.svg`](assets/brand/novafabric-wordmark.svg) | Article headers, slide titles, sponsor walls |
| Social preview (1280×640) | [`social-preview.png`](assets/social-preview.png) | Link previews, blog hero images |
| Terminal demo | [`demo.svg`](assets/demo.svg) | Showing what the tool actually does |

### Palette

Dark is the default theme. Signal-lime is the single hero accent — used sparingly.

| Role | Hex |
|---|---|
| Background | `#0a0a0c` |
| Background (raised) | `#111114` |
| Text | `#e6e6ea` |
| Text (muted) | `#a8a8b3` |
| **Accent — signal lime** | **`#c4f0a8`** |
| Sealed / verified (teal) | `#63cad0` |
| Failure (coral) | `#e87d7d` |
| Warning (amber) | `#e8b866` |

### Typography

- **JetBrains Mono** — identity, data, CLI output, headings.
- **Inter** — body prose.

The rule behind it: monospace means identity and data; sans means prose.

### Please do

- Scale the logo proportionally, and give it clear space equal to the height of the "N".
- Use the mark on dark backgrounds. On light backgrounds, keep the ink badge — the
  badge *is* the contrast.
- Recolor nothing. If lime does not work in your medium, use the mark in single-color
  black or white instead.

### Please don't

- Stretch, rotate, add gradients or drop shadows to the mark.
- Rebuild the wordmark in a different typeface.
- Use the logo as *your* product's logo, or in a way that suggests NovaFabric endorses
  you.

---

## Screenshots and demos

- The terminal demo above (`demo.svg`) is the fastest honest illustration: capture →
  validate → replay → diff.
- To generate your own, every command in [Quick start](../README.md#quick-start) runs
  offline with no API key, so you can record a clean session on any machine.
- Please show **real output**. If you need to trim it for space, say so in the caption.

---

## Interviews, talks, and reviews

The maintainer is happy to answer questions, review a draft for factual accuracy
(without asking for editorial changes), or join a podcast or stream.

- **Best channel:** [GitHub Discussions](https://github.com/MSKazemi/novafabric/discussions)
- **Security topics:** follow [SECURITY.md](../SECURITY.md) — please do not open a
  public issue for a vulnerability.

Giving a talk about NovaFabric? Tell us in
[Show and tell](https://github.com/MSKazemi/novafabric/discussions/categories/show-and-tell)
and we will link it from the project.

---

## Citing NovaFabric in academic work

Use [`CITATION.cff`](../CITATION.cff) — GitHub renders a ready-made citation from it
via **Cite this repository** in the sidebar. See also
[For researchers](for-researchers.md), which covers what NovaFabric does and does
*not* solve for a reproducible paper artifact.
