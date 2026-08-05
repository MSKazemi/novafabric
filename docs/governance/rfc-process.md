# The RFC process

**Status:** Adopted 2026-05-07 · **Applies to:** every non-trivial change to NovaFabric

NovaFabric uses a lightweight, Rust-style RFC process for non-trivial changes:
GitHub-based, time-boxed comment windows, two approvers, and an accompanying
architecture decision record when the decision is architectural.

This page is the canonical, authoritative description of that process. If you
need to know how to propose a change, this is the page.

> **Most contributions do not need an RFC.** If you are fixing a bug, adding a
> test, or improving docs, skip this page entirely — go to
> [CONTRIBUTING.md](../../CONTRIBUTING.md) and open a pull request.

---

## When an RFC is required

An RFC is **required** for any change that:

- Adds, removes, or non-additively changes a public schema (Run Capsule, Run
  Capsule subtypes, Evidence Bundle, Asset Spec)
- Adds, removes, or changes the behavior of a CLI command or a default flag
- Introduces a new runtime dependency
- Changes a storage format or backend
- Affects the security posture or the supply-chain trust model
- Changes governance, maintainer criteria, or this process itself
- Adds a new component to the architecture
- Changes the project's licensing, branding, or trademark stance

An RFC is **not required** for:

- Bug fixes
- Refactors that preserve behavior
- Documentation improvements
- New tests
- New optional adapters that do not require core changes
- Patch-version dependency bumps
- New asset-type fields marked `optional: true` that do not break validation for
  existing capsules — these still need PR review

**If you are unsure, do not guess.** [Open a
Discussion](https://github.com/novafabric/novafabric/discussions) and a
maintainer will tell you which channel your change belongs in. Asking is always
cheaper than writing the wrong document.

---

## How to write one

### 1. Discuss informally first

Before writing anything, [open a
Discussion](https://github.com/novafabric/novafabric/discussions) describing the
problem. This serves three purposes:

1. Confirm an RFC is the right channel at all
2. Surface prior work or related RFCs
3. Identify the right reviewers

A maintainer will respond within a week. See [SUPPORT.md](../../SUPPORT.md) for
our full response commitments.

### 2. Copy the template

```bash
cp docs/rfcs/_template.md docs/rfcs/RFC-NNNN-short-name.md
```

Use the next available 4-digit number. Do not skip numbers.

### 3. Fill in every section

Every section in the template is required. Sections that genuinely do not apply
should say so explicitly — *"Not applicable: this is a spec-only change with no
code"* — rather than be left blank. A blank section reads as an oversight; an
explicit dismissal reads as a decision.

### 4. Open a pull request

- **Title:** `RFC-NNNN: <short name>`
- **Body:** short — 2–3 sentences plus a link to the rendered RFC. The
  substantive discussion happens *in the RFC document*; PR comments are for
  spelling, formatting, and link correctness.
- **Label:** `rfc`

### 5. Solicit reviewers

Tag at least two:

- One **maintainer** — required for merge
- One **steward or domain expert** — the community sponsor

---

## Lifecycle

```
Draft  →  Active  →  Accepted  →  (later)  Superseded
                  ↘  Rejected
                  ↘  Withdrawn
```

| Status | Meaning |
|---|---|
| **Draft** | Author is still writing. Not open for review. |
| **Active** | Open for review. The comment window is running. |
| **Accepted** | Approved by a maintainer and a sponsor. Implementation can begin. |
| **Rejected** | Reviewed and declined. Do not reopen without new evidence. |
| **Withdrawn** | Author withdrew before a decision. Can be revived as a new RFC. |
| **Superseded** | A later RFC replaces this one. Both stay in the repo. |

---

## The comment window

- **Minimum:** 2 weeks
- **Default:** 2 weeks for routine RFCs, 4 weeks for spec-breaking changes
- **Extensions:** any reviewer may request a one-week extension; the author must
  accept up to two before objecting

The window starts when the PR is marked **Active** (drop the draft status). It
ends when a maintainer closes it with a decision.

---

## The decision

A maintainer closes the comment window with one of:

| Decision | What happens |
|---|---|
| **Accepted** | The RFC is merged. The author is responsible for implementation, or for handing it off explicitly. |
| **Rejected** | The RFC is merged with `Status: Rejected` and a documented rationale. It stays in the repo as decision provenance. |
| **Needs revision** | The PR stays open with explicit feedback. The author revises; the window may be extended. |

**Rejection requires a written rationale.** "I disagree" is not enough. The
rationale must reference the project's stated direction, its non-goals, a prior
RFC, or a specific design problem.

---

## After acceptance

1. **Implementation tracking issue** — a maintainer or the author opens one with
   checkboxes for the milestones.
2. **ADR, if architectural** — see [architecture decisions](../decisions.md).
3. **Schema and spec updates** — a schema change ships with a version bump and a
   migration note.
4. **CHANGELOG entry** — the first release containing the implementation
   references the RFC number.

---

## RFCs and ADRs are different documents

| Document | Captures | Written when | Public? |
|---|---|---|---|
| **RFC** | The proposal, the discussion, and the decision | Before implementation, during the comment window | Yes — [`docs/rfcs/`](../rfcs/) |
| **ADR** | The accepted architectural decision, normalized | After RFC acceptance, before code merges | [Index only](../decisions.md) |

RFCs are deliberative; ADRs are normalized. An accepted RFC produces zero or one
ADRs, depending on whether the decision is architectural.

---

## Process exceptions

The project lead may bypass this process for:

- **Critical security fixes** — the fix lands first, the RFC follows
  retroactively within 7 days
- **Trivial editorial changes** — typo fixes, link updates, formatting

The project lead **may not** bypass the process for any change meeting the
"required" criteria above. If a maintainer believes this has happened
inappropriately, they may file a follow-up RFC challenging the change. That is a
deliberate check, and using it is not considered hostile.

---

## Prior art

Modeled on the [Rust RFC process](https://github.com/rust-lang/rfcs) (the primary
inspiration), [Python PEPs](https://peps.python.org/) (structured-document
discipline), and [Kubernetes KEPs](https://github.com/kubernetes/enhancements)
(a simplified version of the graduation-stage model).

Deliberately **not** copied: ECMAScript TC39 stages (too process-heavy for a v0.x
project), the W3C Recommendation track (too consensus-heavy), and IETF
Internet-Drafts (too document-format-heavy).

---

## See also

- [CONTRIBUTING.md](../../CONTRIBUTING.md) — the everyday path, no RFC needed
- [GOVERNANCE.md](../../GOVERNANCE.md) — roles, voting, and decision-making
- [Maintainer criteria](maintainer-criteria.md) — how merge rights are earned
- [Architecture decisions](../decisions.md) — the ADR index
