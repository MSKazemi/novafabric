# NovaFabric Console 2.0 — GUI/UX modernization plan

**Architecture decision reopened 2026-11-22:** the owner welcomes a major frontend redesign. `Astro + React` is a measured baseline, **not a mandatory final architecture**. Compare a React/Vite replacement shell and unified topology experience under [architecture/versions spec](console-2-architecture-and-versioning.md) and issues #262/#263/#264; retain Python runtime, CLI and evidence contracts. This note supersedes any categorical “do not rewrite Astro” language below.

**Status:** proposed, design-only. **Reviewed:** 2026-11-21, Europe/Rome. **Base reviewed:** `main` at `4f89480c2de83f0f51003cb096db58b9904a4ce5`. Refresh the base and re-audit before implementing. Not a release commitment.

## Intent

Make NovaFabric easier to use as a local-first **AI execution investigation and verifiable-evidence platform**. Preserve the existing dashboard instead of rewriting it. A first-class workflow is **find run → inspect trace → examine replay → compare runs → verify/export evidence**.

One console, three optional layout/navigation presets:
- **Developer Studio (default):** dense run exploration, trace, replay, diff, keyboard shortcuts.
- **Evidence Workbench:** cryptographic verification, provenance, redaction, audit and export.
- **Operations Console:** run/alert overview, policy/incidents, infra, administrators.

These are *proposed* design variants, not separate deployed applications, and not claims that the backend already supports every proposed interaction.

## Verified existing implementation

- `ui/dashboard/` ships **Astro 7 + React 19 + TypeScript + Tailwind CSS 4**.
- `src/components/dashboard/Sidebar.tsx` defines 30 tabs across 7 groups. Tab IDs and `?tab=` deep links are stable and must remain valid.
- `DashboardApp.tsx` includes lazy-loaded tabs, connection handling and command palette.
- `RunsTab.tsx` includes run search/filter, URL state, capsule details and replay-result panes.
- `DiffTab.tsx` already handles aligned, multi-run comparison. `LineageTab` and `EvidenceTab` exist.
- `src/styles/tokens.css` includes dark/light modes, accent presets, status and graph tokens.
- Existing `@tanstack/react-virtual`, React Flow, Lucide, Vitest, Playwright and axe-core are available.
- The embedded dashboard is built into the Python wheel and `nova serve --experimental` exposes it; separate website packaging is governed by ADR-0299.
- The **CLI is canonical**; the dashboard displays CLI-equivalent commands, adheres to server rules and must not imply additional capabilities.
- No actual live-browser audit, full current issue inventory or CI run was executed as part of this planning document.

Sources: [dashboard README](../ui/dashboard/README.md), [user guide](dashboard.md), [source sidebar](../ui/dashboard/src/components/dashboard/Sidebar.tsx), [app shell](../ui/dashboard/src/components/dashboard/DashboardApp.tsx), [frontend package](../ui/dashboard/package.json), [architecture](architecture.md), [AGENTS](../AGENTS.md).

## Technology decisions

| Area | Decision | Notes |
|---|---|---|
| Core | retain Astro + React + TypeScript + Vite-under-Astro | do not create a second SPA |
| Styling | retain Tailwind v4 + existing CSS tokens | extend, no blanket reskin |
| Graph | retain React Flow | compare heavy graph approach only after benchmark |
| Icons/fonts | retain Lucide and bundled Inter / JetBrains Mono | no CDN |
| Run tables | reuse virtualized data paths | evaluate TanStack Table only with evidence |
| Server API | reuse `src/lib/api.ts`, generated dashboard OpenAPI and CLI parity guards | avoid drifting duplicate API layers |
| State | reuse existing hooks first | evaluate TanStack Query only if current caching proves insufficient |
| Diff viewer | reuse existing components | lazy Monaco only after bundle/performance approval |
| Accessibility | Playwright + axe, manual keyboard/screen-reader | target WCAG 2.2 AA |
| Component docs | lightweight inventory first, Storybook only if justified | Tier-A license gate |
| Desktop | defer Tauri | web and offline packaging first |

## Information architecture

The main presentation groupings should become **Overview, Investigate, Evidence & Trust, Operations, Advanced Tools**. They wrap/reorder the existing tab IDs; they must not break deep links or CLI parity.

Each workspace should have:
- Top bar with connection state and current environment/capabilities;
- contextual header with concise goal and primary safe action;
- drill-down to original source and CLI equivalent;
- consistent loading, missing, partial, truncated, denied and error states;
- keyboard and screen-reader navigation; reduced-motion and responsive behavior.

### Critical feature requirements

| Surface | Already has | Target improvement | Acceptance |
|---|---|---|---|
| Onboarding | connect panel | first run walkthrough / honest sample mode | no ambiguous demo vs real data |
| Overview | dashboards/analytics | source-aware meaningful status metrics | no fake or non-computable numbers |
| Run Explorer | query/filter, saved views, inspector | easier dense search and selected context | exact run from deep link works |
| Trace/Forensics | views and timeline | linked event inspector and time navigation | ordered observations ≠ asserted causality |
| Replay | replay panels/modes | explicit dedicated workflow and mode explanation | forensic ≠ live replay; side effects gated |
| Diff | structural and multi-run compare | better aligned change explorer | server-authoritative changed/added/removed |
| Lineage | React Flow DAG | bounded, accessible provenance focus | relationship semantics remain accurate |
| Evidence | bundle verification/seal/export | step-by-step signer/integrity/trust status | valid signature ≠ compliant/correct |
| Capture | CLI equivalent/command builder | guided safe setup | no arbitrary server execution without security approval |
| Governance | existing panels | source-linked policy decisions | authz enforced by server |
| Operations | alerts/infra/admin | alert → run → evidence workflow | clear permissions and completeness |
| Advanced | specialist tabs | searchable feature directory | all legacy capabilities preserved |

## Implementation stages

1. **UX-00 baseline and technical/issue audit (P1):** inventory current screens, exact API/CLI parity, historic defect status, current CI, screenshots, realistic dataset, test gaps and previous ADRs. No redesign before this.
2. **UX-01 user journeys (P1):** novice developer, agent engineer, researcher, auditor, operator; document task scripts, error cases and qualitative research.
3. **UX-02 prototypes (P1):** three distinct but compatible Developer/Evidence/Operations designs; desktop, tablet, mobile; dark/light; clickable walkthrough, concept evaluation.
4. **UX-03 design system (P1):** reuse existing components and tokens, standardize states, focus, density and responsive split panes.
5. **UX-04 frontend foundation (P1):** workspace switcher, shared investigation context, preserved old URLs and shortcuts; no new backend execution semantics.
6. **UX-05 run/trace vertical slice (P1):** find a run, inspect an event and follow source metadata.
7. **UX-06 replay workflow (P1):** explain supported replay modes and results without overstating re-execution.
8. **UX-07 diff workflow (P1):** A/B/N comparisons with source-linked server verdict.
9. **UX-08 evidence workflow (P1):** cryptographic checks, independence and export.
10. **UX-09 lineage (P2):** focus navigation, performance caps and accessible graph/table fallback.
11. **UX-10 operations (P2):** role-aware alert/incident/infra drill-down using existing backend permissions.
12. **UX-11 hardening (P1 gate):** XSS, tokens, in-band failure, completeness, keyboard/a11y, real-server and load tests. Do each slice's tests throughout implementation.
13. **UX-12 packaging and release (P1 gate):** clean wheel, offline, static asset integrity, docs, release notes and rollback.

### Proposed releases (frontend labels only)

- **Console 2.0:** developer investigation workflow and UI foundation.
- **Console 2.1:** evidence workbench and provenance navigation.
- **Console 2.2:** operations workspace and multi-user experiences.

No target dates or project-status percentages have been assigned.

## Verification and security contracts

- Never report forensic read-only inspection as true deterministic reproduction; replay modes and external effects require explicit affordances and backend safety.
- No synthetic proof, redaction status, file sizes or claims are shown as live evidence. Always indicate source, scope and completeness.
- Cryptographic integrity, signer identity/trust, timestamp and legal/policy status are different checks.
- No new mandatory network dependency, analytics, telemetry, cloud service or external CDN.
- Local mode must use SQLite and run offline, with no PostgreSQL precondition.
- No change to Run Capsule / Evidence Bundle schema without the normal RFC process.
- No exposure of local serve tokens in logs or shared URLs; review existing session handling before changing.
- Reconcile historic frontend review findings with current code instead of duplicating previously fixed issues.
- Retain stable tab IDs, deep links, filter grammar, command mapping, and wheel packaging.
- New server routes require auth, permissions and route/CLI parity checks.
- Keep private strategy and research out of public documentation.

### Quality gates

In `ui/dashboard/`: `npm ci`; `npm run lint`; `npm run test:unit`; `npm run test:e2e`; `npm run test:live`; `npm run test:visual`; `npm run test:a11y`; `npm run check:licenses`; `npm run build:dashboard`. Some live tests depend on browser/server availability—record skipped tests, do not call them passed. Add dashboard-specific axe tests: the inspected `tests/e2e/a11y.spec.ts` currently enumerates legacy Astro showcase routes, not the actual `/dashboard/` shell.

For affected repository code: `make test-fast`, `make lint`, `make typecheck`, `make check-links`; for release/packaging, clean Python wheel install and server smoke test. Any performance improvements must be based on measured before/after representative data rather than a claimed percentage.

### Usability study and success criteria (targets, NOT measured)

- First failed run located within 60 s.
- Two-run comparison completed within 90 s.
- Evidence verification outcome found within 60 s.
- >=90% task completion in a moderated pilot; capture qualitative errors and incorrect certainty claims.
- No critical a11y violations, no regression in old deep links/CLI parity, no unsupported capability presented as working.

## GitHub implementation policy

Work from existing issues and existing Project V2 if available; deduplicate before creating additional epic/issue records. Each issue needs outcome, paths/scope, prior evidence, dependencies, acceptance checklist, test command and owner role. Use focused draft PRs. Preserve existing files, migration gates, security posture and human approval.

### First Claude Code work packet (audit, not implementation)

**Goal:** review current `ui/dashboard` and `docs/dashboard.md` against the proposed UX; capture a real frontend baseline and three design alternatives, map implementation gaps and open a scoped draft PR with evidence. Read `AGENTS.md`, `CONTRIBUTING.md` and applicable ADRs first. Do not rewrite frontend, change backend/security, merge, deploy, add external telemetry or publish confidential information. Attach screenshots, audit matrix, actual test logs and open questions. The first implementation issue should be approved only after the design and security gates are understood.

## Backout

Every change must be reversible by reverting its focused PR/bundle. Preserve existing saved views and user-authored dashboard widget files. Changes to authorization, schema or persistence need a separate approved migration and rollback plan rather than an ordinary visual redesign.

---

<!-- console2-live-epic-reconciliation-20261122 -->
## Live GitHub execution structure (2026-11-22)

This public roadmap is in draft PR #251. **Canonical umbrella:** [#252](https://github.com/MSKazemi/novafabric/issues/252). Instead of multiple competing Console programs, execution is organized into six public **workstream epic issues** with specific Claude Code prompts, acceptance gates and task links:

| Epic | Existing implementation issues | Gate |
|---|---|---|
| [#275 UX research / onboarding](https://github.com/MSKazemi/novafabric/issues/275) | #253, #254, #265, #266 | D0 user/design acceptance |
| [#272 Frontend platform / architecture / versioning](https://github.com/MSKazemi/novafabric/issues/272) | #262, #263, #267, #283 (#266 is related W1 navigation) | D1 accepted ADR + API/capability contract |
| [#277 Developer Studio](https://github.com/MSKazemi/novafabric/issues/277) | #255, #268, #269, #270 | Console 2.0 core |
| [#279 Evidence and lineage](https://github.com/MSKazemi/novafabric/issues/279) | #271, #274 | 2.0 trust / 2.1 lineage |
| [#281 Operations](https://github.com/MSKazemi/novafabric/issues/281) | #276 | Console 2.2 |
| [#284 Release quality](https://github.com/MSKazemi/novafabric/issues/284) | #261, #264, #278, #280, #282 | Every release |

**Shared cross-workstream #256** stays under root #252 to coordinate W4/W5 without double implementation. **Optional P3 research #285** stays in backlog. External #250 (native Project #14), #260 scale harness and #211/#220/#225 existing release work are linked dependencies, not new Console-owned replicas.

**Important correction to the earlier technology table above:** retaining Astro is a *baseline option*, **not** an accepted decision. Strong architecture changes are explicitly authorized for comparative research under [architecture and versioning](console-2-architecture-and-versioning.md) and #262. Any major app replacement still preserves the Python backend, offline wheel and immutable evidence contract and needs an accepted ADR.

**Release identity:** Python package is installable product release; Console 2.0/2.1/2.2 are experience labels; UI bundle uses source SHA/digest and explicit API capabilities; no untested standalone frontend↔backend compatibility promise. Preserve N/N+1 database migration contract and independently versioned Run Capsules/Evidence Bundles.

**Board status:** all epics/issues and issue-body mappings exist, but GitHub connector cannot write native Project V2 parent/dependency edges, priorities or milestones. [#250](https://github.com/MSKazemi/novafabric/issues/250) contains the exact requested hierarchy to reconcile **existing** [Project #14](https://github.com/users/MSKazemi/projects/14), without making a new Project or claiming unverified native links.
