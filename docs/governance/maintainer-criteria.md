# Maintainer criteria

How merge rights are earned, what they commit you to, and how to step back.
This page is deliberately explicit: an unwritten path to maintainership is a
path only insiders can walk.

Current maintainers and stewards are listed in
[MAINTAINERS.md](../../MAINTAINERS.md).

---

## What a maintainer is

A contributor with **merge rights** on the main repository, trusted to:

1. Review and merge changes within their area of competence
2. Triage incoming issues with consistent priority labels
3. Vote on RFCs — one maintainer plus one community sponsor are required to
   merge one
4. Uphold and enforce the [Code of Conduct](../../CODE_OF_CONDUCT.md)
5. Represent the project responsibly in public

**Maintainers are not employees.** They are not required to respond on a
schedule. They are expected to set a realistic capacity and to step back
formally rather than disappear.

---

## The path

### Step 1 — Earn trust through contribution

There is no fixed threshold. A typical path:

- **At least 3 non-trivial PRs merged** — more than a typo fix, demonstrating
  understanding of the codebase
- **Sustained activity over 3+ months** — not a single burst
- **Consistent participation in code review** — thoughtful comments on *others'*
  PRs, not only your own
- **Visible problem ownership** — taking issues from triage to resolution,
  including tests, documentation, and the changelog entry

### Step 2 — Demonstrate judgment in design discussions

Beyond shipping code:

- Has filed or co-authored at least one [RFC](rfc-process.md)
- Has identified design issues in others' RFCs that survived discussion
- Can disagree constructively in review without escalating

These signals matter more than line counts. A contributor who merges a lot of
code but always defers on architecture is not yet ready.

### Step 3 — Self-nominate or be nominated

Either works, and **self-nomination is genuinely welcome** — open an issue
titled `Maintainer nomination: @<handle>` with a brief case: merged
contributions, RFCs participated in, areas of focus, and available bandwidth.

### Step 4 — The vote

Until a technical steering committee exists (targeted v0.7+), the project lead
approves new maintainers after soliciting feedback from existing maintainers and
stewards. After the TSC exists, it votes by simple majority; the project lead
retains a nomination right but not a veto.

**The vote is public.** Each vote and its brief rationale is posted in the
nomination issue. Confidential concerns may be raised privately with the project
lead, who summarizes them anonymously in the public thread.

### Step 5 — Onboarding

An accepted maintainer is added to [MAINTAINERS.md](../../MAINTAINERS.md) and to
the GitHub team with merge rights, and is given access to the security-issue
channel.

---

## What maintainers commit to

**Time**

- Best-effort review within their area — target 1 week, hard ceiling 2 weeks
  before it is fair to ping them
- A quarterly check-in confirming continued availability before each minor
  release
- Announcing a leave rather than going quiet

**Quality**

- Don't merge your own code without review, except trivial fixes documented in
  [the release process](../release-process.md)
- Don't merge breaking changes without an [RFC](rfc-process.md)
- Don't bypass CI — if CI is broken, fix CI first

**Conduct**

- Uphold the Code of Conduct, including in private interactions
- Disclose conflicts of interest — if your employer asks you to push a feature,
  say so
- Respect contributor time: feedback should be timely, PRs should not rot

---

## What maintainers do *not* commit to

Stated explicitly, because unstated expectations are how maintainers burn out.
Maintainers are **not** expected to:

- Provide free user support beyond issue triage
- Respond to direct messages or email — use issues and discussions
- Defend the project against bad-faith critics
- Work on any fixed schedule
- Maintain expertise across the whole codebase — an area of competence is enough
- Travel for the project unless it is paid for and they want to

---

## Areas of competence

Maintainers self-declare areas in [MAINTAINERS.md](../../MAINTAINERS.md).
Reviewing and merging outside your declared areas requires a co-review from
someone inside them.

| Area | Code |
|---|---|
| Capture & Replay | `src/novafabric/capture/`, `src/novafabric/replay/` |
| Asset Registry | `src/novafabric/spec/`, `src/novafabric/registry/` |
| Lineage & Evidence | `src/novafabric/lineage/`, `src/novafabric/evidence/` |
| CLI | `src/novafabric/cli/` |
| Adapters & Integrations | `src/novafabric/adapters/`, `integrations/` |
| Server & Metadata Store | `src/novafabric/server/`, `src/novafabric/metadata_store/` |
| Dashboard | `web/` |
| Documentation & Spec | `docs/`, `schemas/`, RFC stewardship |

---

## Stepping back

Maintainers may step back at any time:

1. Open an issue announcing the change
2. Update [MAINTAINERS.md](../../MAINTAINERS.md), or ask another maintainer to
3. Optionally accept **emeritus** status — recognized as a former maintainer
   without active responsibilities

Emeritus maintainers keep commit-history credit and may rejoin without
re-applying.

---

## Removing a maintainer

A maintainer may be removed for:

- **Sustained inactivity** — 6+ months with no commits, reviews, or responses,
  after a 30-day notice
- **Code of Conduct violation** — per the CoC enforcement process
- **Loss of trust** — by two-thirds vote of the remaining maintainers or the TSC

Removal is rare. The default for an inactive maintainer is emeritus status, not
removal. Removal for cause is reserved for serious cases and is documented
privately.

---

## Don't want merge rights?

Many excellent contributors would rather ship code than own review duties. They
are recognized as **stewards** in [MAINTAINERS.md](../../MAINTAINERS.md).
Stewards:

- Vote on RFCs as the community sponsor — the second required approval
- Are listed in the credits
- Can become maintainers later if they change their mind

Steward status is granted on the same informal criteria: sustained quality
contribution and good judgment in design discussions.

---

## Growth targets

Honest about where the project is: it is currently led by a single founder and
is actively recruiting. These are targets, not accomplishments.

| Phase | Maintainer target | Steward target |
|---|---|---|
| v0.2 → v0.3 (project lead) | 1 (founder) + 1–2 stewards | 2–3 |
| v0.3 → v0.5 (recruiting) | 1 + 2 external | 3–5 |
| v0.5 → v0.7 (foundation-ready) | 3–4 | 5–7 |
| v0.7+ (TSC) | TSC of 3–5, maintainer pool of 5–10 | 10+ |

---

## See also

- [CONTRIBUTING.md](../../CONTRIBUTING.md) · [GOVERNANCE.md](../../GOVERNANCE.md)
- [RFC process](rfc-process.md) · [Design partners](design-partners.md)
