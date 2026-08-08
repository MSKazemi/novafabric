# Design partners

**Status: open — 0 of 5 cohort-1 slots filled, 0 of 3 independent sign-offs on
the v1.0 spec.** If you run real AI or HPC workloads and want a say in a format
you will have to live with, this is the moment where your input changes the
outcome.

A **design partner** is an organization that adopts NovaFabric in a real
workflow before v1.0 and trades regular feedback for direct influence over the
spec. It is not a customer relationship — the project has no commercial offering
and design-partner status is free.

---

## Why the program exists

The Run Capsule format and the Replay Engine contract are the two artifacts
NovaFabric is most committed to. **If they are frozen at v1.0 against an imagined
user instead of a real one, the project fails** — the format will not survive
contact with production workloads.

The program exists to prevent exactly that:

- Surface real capture/replay scenarios *before* they arrive as bug reports
  against a 1.0 release
- Stress the schema against workloads the maintainers have no access to —
  large-scale HPC, regulated data, agent frameworks we don't run
- Build a citable adoption record

**Freezing the v1.0 spec requires 3 independent design-partner sign-offs.** The
maintainers' own validation explicitly does not count toward that number. This is
the single largest gate on v1.0, and no amount of engineering unblocks it.

---

## The first cohort — deliberately small, deliberately diverse

| Slot | Profile | Why this profile |
|---|---|---|
| 1 | **University HPC lab** running ML on a SLURM cluster | Validates the SLURM runner; HPC is the project's distinct wedge |
| 2 | **National lab or research institute** with multi-cluster workflows | Tests the K8s↔SLURM bridge; brings audit scrutiny |
| 3 | **AI startup shipping LLM agents** in production | Validates capsule fidelity for agent workflows and the framework adapters |
| 4 | **Enterprise platform team** in a regulated industry | Validates the Evidence Bundle and the threat model against real compliance needs |
| 5 | **Open-source agent-framework author** | Validates the spec direction; brings ecosystem credibility |

We do not approach any vendor whose product directly competes with a NovaFabric
primitive.

---

## What you commit to

**Adoption** — use NovaFabric in a real workflow within 90 days of joining;
provide a written workflow description (kept private); share at least 5 real
capsules per quarter, redacted as needed, for spec validation.

**Feedback** — one 30-minute call per quarter with the project lead;
asynchronous feedback on RFCs affecting your workflow within the comment window;
a reasonable response to direct spec-review asks, target one week.

**Authorization** — a named engineer permitted to participate in RFCs and code
review on company time.

**Conduct** — your organization and its representatives uphold the
[Code of Conduct](../../CODE_OF_CONDUCT.md).

---

## What you get

**Influence on the spec**

- Your feedback is explicitly weighted in RFC review alongside maintainer
  comments when judging consensus
- Requested features get a `dp-requested` label and rise in roadmap planning
- Your representatives may co-author RFCs without being maintainers

**Early access** — release candidates two weeks ahead of public release, and a
direct channel for workflow questions.

**Recognition** — listed with your logo (opt-in) in the README and on the
project website; cited in foundation applications; invited to co-present.

**What you explicitly do not get** — a discount or revenue share (there is
nothing to discount); veto rights (final say stays with the project lead and
later the TSC — your feedback is input, weighted input, but input); source rights
beyond the public Apache-2.0 license; private features or forks.

---

## How to apply

Email **[design-partners@novafabric.io](mailto:design-partners@novafabric.io)**
with:

- Organization name
- Your primary use case, one or two paragraphs
- A named engineering representative and their availability
- Any NovaFabric capability you need or are currently blocked on

The project lead reviews and either accepts, asks for more information, or
declines with reasons. **First response within 2 weeks.**

Not ready to commit an organization? [Open a
Discussion](https://github.com/MSKazemi/novafabric/discussions) instead —
informal feedback on the spec is welcome from anyone and needs no paperwork.

---

## See also

- [RFC process](rfc-process.md) — how spec changes get proposed and decided
- [GOVERNANCE.md](../../GOVERNANCE.md) — roles and decision-making
- [ROADMAP.md](../../ROADMAP.md) — what v1.0 still needs
