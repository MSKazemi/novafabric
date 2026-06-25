# Governance

**Status:** Adopted (v0.2)
**Adopted:** 2026-05-07

This document describes how decisions are made in NovaFabric. The trajectory
from BDFL → TSC → LF AI & Data Foundation is described in
[`design/strategy/foundation-ready-governance.md`](design/strategy/foundation-ready-governance.md).
This document covers the **current** state and the rules that apply today.

---

## Roles

### Founder / BDFL

Owns the product vision, has final say on contested decisions, and is
responsible for the project's direction through v0.7. Listed in
[`MAINTAINERS.md`](MAINTAINERS.md).

The BDFL phase ends when 2+ external co-maintainers have demonstrated
sustained merge-quality work and the project no longer depends on any single
person to ship.

### Maintainers

Hold merge rights on the main repository. Currently founder-only; the path
for external maintainers is documented in
[`design/governance/MAINTAINER_CRITERIA.md`](design/governance/MAINTAINER_CRITERIA.md).

Maintainer responsibilities:
- Review and merge PRs in their area of competence
- Triage issues with consistent priority labels
- Vote on RFCs (one maintainer + one community sponsor required to merge an RFC)
- Uphold the Code of Conduct

### Stewards

Trusted contributors with deep area knowledge but without merge rights. Vote
in RFCs as community sponsors. Listed in `MAINTAINERS.md` under "Stewards".

### Contributors

Anyone who opens an issue, comments on a discussion, submits a PR, or files
an RFC. The bar for first contribution is low; the bar for becoming a
maintainer is documented separately.

### Design Partners

Organizations using NovaFabric in production before v1.0 in exchange for
input on the spec direction. Listed in
[`design/governance/DESIGN_PARTNERS.md`](design/governance/DESIGN_PARTNERS.md).
Their engineers are first-class community members; their organizations have
no special rights beyond input timing.

---

## Decision-making

Decisions are categorized by reversibility and blast radius.

### Local decisions

Bug fixes, refactors, doc improvements, new tests, new examples, dependency
patch bumps. **Any maintainer may merge** after a code review.

### Reversible decisions

New CLI flags, new optional adapters, new asset-type fields, new schema
fields with `optional: true`. **Maintainer + 1 reviewer**, 24-hour window for
objections.

### Non-trivial / architectural decisions

Anything that:
- Changes a public schema in a non-additive way
- Introduces a new dependency
- Changes the CLI surface (commands, default behavior)
- Adds a new component to the architecture
- Changes a data format or storage backend
- Affects security posture or supply-chain trust

Requires an **RFC** (see `design/governance/RFC-0000-rfc-process.md`). Merge
requires:
- 2-week comment window
- 1 maintainer approval + 1 community sponsor (steward or maintainer)
- An accompanying ADR if architectural

### Breaking changes

Any change that breaks a published Run Capsule, Evidence Bundle, or CLI
contract. Requires:
- An RFC
- BDFL approval (until TSC exists; then TSC majority)
- A documented migration path
- A deprecation period of at least one minor release

### Emergency decisions

Critical security fixes may bypass the RFC process. The maintainer who lands
the fix files a retroactive ADR within 7 days.

---

## Voting

Until the TSC exists (v0.7+), formal voting is BDFL-final. RFCs may have
non-binding votes from maintainers and stewards; the BDFL weighs them and
publishes a written rationale for any decision that goes against the majority.

When the TSC exists:
- Each TSC member has one vote
- Quorum is 3 of 5 members
- Simple majority for routine decisions
- 2/3 majority for spec-breaking changes
- Tie broken by the chair (rotating annually)

---

## Release cadence

| Release type | Cadence | Owner |
|---|---|---|
| Patch (`v0.X.Y`) | As needed for bug fixes | Any maintainer |
| Minor (`v0.X`) | Quarterly target | BDFL approves |
| Major (`vX`) | Annually or by spec milestone | BDFL approves; TSC after v0.7 |

Release process is documented in [`docs/release-process.md`](docs/release-process.md).

---

## Code of Conduct enforcement

Reports go to [security@novafabric.io](mailto:security@novafabric.io) (or the
address documented in `CODE_OF_CONDUCT.md`). Initial response within 72 hours.

The CoC committee for v0.2-v0.7 is the BDFL plus one steward. All decisions
are documented privately and summarized publicly at year-end.

After TSC formation, CoC enforcement transitions to a 3-person committee
elected by the TSC, including at least one non-maintainer community member.

---

## Conflict resolution

If a maintainer disagrees with a merged PR, the resolution path is:

1. **Discuss in the PR thread.** Most disagreements resolve here.
2. **Open a follow-up RFC** if the disagreement is architectural.
3. **Escalate to BDFL** (or TSC after v0.7) if discussion does not converge.
4. **Public post-mortem** if the conflict is symptomatic of a process gap.

There is no escalation channel above the BDFL/TSC. Foundation governance
(after v1.0) provides one through LF AI & Data TAC.

---

## Trademark and brand

The "NovaFabric" name and any associated logo are owned by the founder until
trademark is formally registered. Registration is on the v0.7 milestone
checklist; the trademark will be assigned to LF AI & Data on Sandbox
acceptance.

Until then, third-party use of the NovaFabric name to identify forks,
derivatives, or unrelated projects is discouraged. Use of the name to
**describe** integrations ("X works with NovaFabric") is welcome.

---

## Funding and resources

NovaFabric is not VC-funded. Sustainability comes from:

- GitHub Sponsors / Open Collective (best-effort)
- Contributor employers (design partners and others contributing on
  company time)
- Optional service revenue (training, consulting, enterprise support)

Funds, when received, go to:
1. CI/CD costs
2. Domain registration and trademark
3. Conference travel for maintainers
4. Maintainer time (when financially material)

Hosted SaaS is **not** part of the funding plan (see
[`design/strategy/non-goals.md`](design/strategy/non-goals.md)).

---

## Bus factor

The project must not depend on a single person past v0.7. Concretely:

- By v0.5: 2 external co-maintainers with merge rights.
- By v0.7: TSC with 3 voting members.
- By v1.0: LF AI & Data Sandbox application submitted.

If at any point the BDFL becomes unavailable for >30 days without prior
notice, the most-senior steward (longest tenure) becomes interim BDFL until
the BDFL returns or the TSC forms.

---

## Amending this document

Changes to GOVERNANCE.md require an RFC. Until v0.7, the BDFL has final
approval; after v0.7, the TSC does. This is intentional: governance changes
are exactly the decisions that require deliberate process.

---

## See also

- [`MAINTAINERS.md`](MAINTAINERS.md) — current maintainers and stewards
- [`design/governance/RFC-0000-rfc-process.md`](design/governance/RFC-0000-rfc-process.md) — RFC process
- [`design/governance/MAINTAINER_CRITERIA.md`](design/governance/MAINTAINER_CRITERIA.md) — path to merge rights
- [`design/governance/DESIGN_PARTNERS.md`](design/governance/DESIGN_PARTNERS.md) — design partner program
- [`design/strategy/foundation-ready-governance.md`](design/strategy/foundation-ready-governance.md) — long-term trajectory
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — community baseline
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to participate
