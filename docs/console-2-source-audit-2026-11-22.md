# Console 2.0 — Source-Based Frontend Audit (2026-11-22)

> **Status: PARTIAL AUDIT — source inspection performed, runtime/browser benchmarks NOT performed.**  
> Issue: [#253](https://github.com/MSKazemi/novafabric/issues/253); program: [#252](https://github.com/MSKazemi/novafabric/issues/252); design proposal: [draft PR #251](https://github.com/MSKazemi/novafabric/pull/251).  
> Reviewed public default branch `main` at commit `4f89480c2de83f0f51003cb096db58b9904a4ce5` on 2026-11-22 (Europe/Rome). Connector read-only inspection; this audit did not execute the repository, run CI or operate a live browser. Private background design was checked separately, but no confidential information is reproduced here.

## Summary and decisions

**Do not rebuild the frontend from scratch.** The existing application is substantial and already contains most core technical panels. Best next investment: simplify information architecture, connect previously isolated workflows, improve clarity of replay/evidence semantics, and validate performance/accessibility using actual users.

**Keep existing stack:** Astro 7 + React 19 + TypeScript + Tailwind CSS 4 + React Flow; retain bundled font/assets and wheel-packaged dashboard. Three experience variants should be design/nav presets over one application: Developer Studio (default), Evidence Workbench and Operations Console. All proposals are subject to usability pilot and maintainer review.

**Do not call this a live UX baseline:** no locally running NovaFabric server, captured task timings, screenshots of the shipped dashboard, Lighthouse results, browser network measurements, or CI pass evidence were obtained. These are explicit outstanding acceptance items for #253.

## Verified inventory and source anchors

| Area | Verified current implementation | Source |
|---|---|---|
| App framework | Astro 7.4.2, `@astrojs/react` 7, React 19, TS 7, Tailwind 4; Astro uses Vite internally | [package.json](../ui/dashboard/package.json), [astro.config.mjs](../ui/dashboard/astro.config.mjs) |
| UI modules | `Sidebar.tsx` union of **30 frozen tab IDs**, seven labeled nav groups, `g`-sequence shortcuts and persistent collapse | [Sidebar.tsx](../ui/dashboard/src/components/dashboard/Sidebar.tsx) |
| Shell | one `client:only="react"` dashboard, lazy component imports, tab URL state, command palette, connection panel, error boundaries | [dashboard.astro](../ui/dashboard/src/pages/dashboard.astro), [DashboardApp.tsx](../ui/dashboard/src/components/dashboard/DashboardApp.tsx) |
| Domain screens | Runs, Diff, Capture, Lineage, KG, Evidence, Seal, Audit, Compliance, Admin, Infra, Alerts, Cost and others already exist | [Sidebar.tsx](../ui/dashboard/src/components/dashboard/Sidebar.tsx), [docs/dashboard.md](dashboard.md) |
| Run Explorer | searchable/filterable Runs view, saved view compatibility, selected run URL state, capsule/trace/forensics/replay result panes | [RunsTab.tsx](../ui/dashboard/src/components/dashboard/tabs/RunsTab.tsx), [RunInspector.tsx](../ui/dashboard/src/components/dashboard/tabs/runs/RunInspector.tsx) |
| Diff | `DiffTab.tsx` calls a backend diff API, canonical summary fallback and `TraceDiffGraph` | [DiffTab.tsx](../ui/dashboard/src/components/dashboard/tabs/DiffTab.tsx) |
| Visual system | tokens for dark/light, green accent and alternatives, edge relationship colors, motion/spacing/fonts | [tokens.css](../ui/dashboard/src/styles/tokens.css) |
| API | hand-maintained typed `api.ts`, server-client connection and managed run SSE; dashboard OpenAPI generated separately | [api.ts](../ui/dashboard/src/lib/api.ts), [API contract](../api/openapi-dashboard.yaml) |
| Authorization | scoped dashboard policy code (`read/operate/admin/audit`) plus enforcement tests are present; older claims of zero authorization are stale | [authz.py](../src/novafabric/serve/authz.py), [test_authz_enforcement.py](../tests/serve/test_authz_enforcement.py) |
| Reliability | source contains code splitting, managed SSE reconnect, per-tab error boundaries, honest aggregate refusal object | [DashboardApp.tsx](../ui/dashboard/src/components/dashboard/DashboardApp.tsx), [api.ts](../ui/dashboard/src/lib/api.ts) |
| Server scaling | 100k-row **in-process endpoint** latency gate documented; it does **not** benchmark browser rendering/network | [dashboard-scale.md](ops/dashboard-scale.md) |
| Packaging | separate dashboard build `npm run build:dashboard` with explicit static asset copy into Python wheel, no public-site build assets | [ui/dashboard/README.md](../ui/dashboard/README.md), [copy-dashboard.mjs](../ui/dashboard/scripts/copy-dashboard.mjs) |
| Accessibility/test commands | Vitest, Playwright E2E/visual/live, axe; `test:a11y` points to a route list containing legacy public-site/showcase routes | [package.json](../ui/dashboard/package.json), [a11y.spec.ts](../ui/dashboard/tests/e2e/a11y.spec.ts) |

### Exact 30-tab nav inventory and proposed destination map

These IDs **must not change**; `?tab=`, shortcuts and CLI parity depend on them. Reclassification is purely a view-layer proposal.

| Existing group | Existing IDs | Proposed new group |
|---|---|---|
| Overview | `home`, `analytics`, `cost`, `dashboards` | Overview |
| Runs & Debug | `runs`, `diff`, `capture` | Investigate |
| Govern & Promote | `registry`, `governance`, `eval`, `risk`, `policy`, `holds` | Operations / Advanced Tools |
| Provenance & Trust | `lineage`, `kg`, `evidence`, `seal`, `spine`, `audit` | Investigate / Evidence & Trust / Advanced Tools |
| Compliance | `compliance`, `incidents`, `schema` | Evidence & Trust / Operations / Advanced Tools |
| Platform | `infra`, `storage`, `ops`, `alerts`, `admin`, `commands` | Operations / Advanced Tools |
| Reports & Export | `reports`, `export` | Overview / Evidence & Trust |

Proposed workspace presets share one accessible tool directory so that expert functions cannot disappear. Workspace selection is a user preference, **not** a different authorization scope or independently maintained UI application.

## Findings prioritized by user impact

**F-01 (P1, observed source): first-run information overload.** Thirty specialist tab IDs are organized into seven groups; current sidebar defaults collapse groups for scanning. A five-destination contextual navigation layer may reduce discovery friction while keeping old tab IDs. **Not established:** user-completion improvement. Needs A/B usability tasks and measured results.

**F-02 (P1, observed source): workflow fragmentation.** Runs, Diff, Evidence, Seal and Lineage have separate top-level panels. Runs also embeds trace and replay subpanes. A consistent selected-run investigation context and cross-links could reduce repeated selection without another backend store. **Caution:** preserve URL-backed filtering, transient replay result state and existing safety checks.

**F-03 (P1, observed test configuration): dedicated dashboard accessibility evidence is incomplete in the invoked test script.** `npm run test:a11y` executes `tests/e2e/a11y.spec.ts` whose explicit `ROUTES` enumerate `/`, `/concepts`, `/install`, `/why`, `/spec` and `/showcase/*`, not `/dashboard/`. This **does not prove** the embedded dashboard has accessibility defects or lacks all other coverage. Propose dashboard-specific axe+keyboard tests and document any disabled rules.

**F-04 (P1, observed source; security review required): browser token handling needs explicit threat-model verification before adding new navigation/share behavior.** `api.ts` currently stores the local serve token in `localStorage`; `DashboardApp.openTopology` uses a token-bearing `?token=` URL; the managed SSE stream also uses token query parameters. This is source-observed behavior, **not a demonstrated exploit**. Review origin isolation, referrer/history behavior, CSP, XSS boundaries, server binding, token expiry and whether any secure alternative is compatible with local mode. Treat any change as an RFC/security review, not a cosmetic UX change.

**F-05 (P1, observed documentation): browser performance baseline is missing from the server scale contract.** `docs/ops/dashboard-scale.md` exercises in-process FastAPI TestClient at 100k indexed rows, not network/React/graph rendering. Baseline cold start, interaction responsiveness, long trace, memory/GC and large graph at laptop performance. No performance % can be claimed from existing gate alone.

**F-06 (P2, observed source): governance and verification terminology is broad.** There are dedicated Evidence, Seal, Audit, Governance, Compliance and Operations panels. A cross-workspace information/label system should distinguish observed, derived, not computable, truncated, untrusted signer, signature valid, timestamp unknown, policy violation and verified integrity. A UI badge cannot prove overall agent correctness or legal compliance.

**F-07 (P2, observed source): design-system consolidation opportunity.** Tailwind tokens, dark/light/accent modes and shared UI primitives already exist. Audit actual component variations before adopting shadcn/ui, Monaco, TanStack Query or new graphics libraries. No dependency additions without license, wheel-size and compatibility review.

**F-08 (P2, code status audit): historical defects must not be reissued as current bugs without reproduction.** In current source `ReportChart.tsx` builds React SVG nodes instead of injected string markup; `DiffTab.tsx` counts booleans for model/tool pairs; `useMutation.ts` checks `ok:false`; `CapsuleInspector.tsx` has a live redaction proof fetch branch. Historic audit claims from earlier commits cannot be blindly reused. Still require tests on an actual browser/server and check all related edge cases.

## Technical implications

- Preserve `Astro` shell and `npm run build:dashboard`; no standalone Next.js app or new always-on runtime.
- Keep existing `api.ts` as source of frontend integration and OpenAPI generator; later introduce generated types only after parity validation.
- Reuse React Flow, graphology, virtualized lists, existing chart/diff views before adding new stack.
- Keep no telemetry/no external fonts/no CDN/air-gap capabilities and Tier-A transitive license policy.
- Keep filters server-owned, deep links stable, and replay/evidence truthfulness explicit.
- Prefer a read-only prototype over a new execute button in Capture, Replay or Admin.
- Static example data must be labelled synthetic on all prototype screens.

## Unverified (must be completed before closing #253)

| Gate | Required evidence | Result as of this audit |
|---|---|---|
| Browser captures of shipped app | screenshots at 1440/1024/768/390, dark/light; server version | **NOT RUN** (no running repo/server or authenticated browser attached) |
| Frontend test suite | `npm ci`, TS lint, Vitest, E2E/visual/a11y/live | **NOT RUN** |
| Backend test suite | route parity, authz, scale and CLI coverage | **NOT RUN** |
| Interaction benchmarks | cold start, search, inspect, trace, large graph, bundle size on defined device | **NOT MEASURED** |
| Usability test | moderated sessions and task results; baseline vs prototype | **NOT RUN** |
| End-to-end security | actual token leakage tests, mutation refusals, XSS corpus, permissions | **NOT RUN** |
| Current Project V2 status | native project #14 field/board membership | **NOT VERIFIED** (GitHub connector does not expose Project V2 actions) |

A static prototype can proceed as a *research artifact* without claiming these gates passed. Production integration #255 remains blocked until G0/G1/G2/G3 and review.

## Ready-to-execute local verification runbook

From a clean public clone, following [AGENTS.md](../AGENTS.md):

```bash
uv sync --all-extras
cd ui/dashboard
nvm use
npm ci
npm run lint
npm run check:licenses
npm run test:unit
npm run test:e2e
npm run test:visual
npm run test:a11y
npm run test:live
npm run build:dashboard
cd ../..
make test-fast
make lint
make typecheck
make check-links
NOVA_DASHBOARD_SCALE=1 uv run pytest tests/bench/test_dashboard_scale_gate.py -v
```

Some browser/live suites require Chrome and a working local serve fixture; report `SKIPPED` rather than passing when unavailable. For packaging run `make bundle` and install built wheel in a **clean** Python environment, then smoke test `nova serve --experimental` offline. Capture exact logs, environment, base commit, timings, screenshots, exits and release notes.

For the prototype research, run the same tasks against baseline and sample-marked prototype at the same resolution. Keep task order counterbalanced and track time/accuracy; no real PII or production tokens.

## Recommended next actions (no implementation yet)

1. Local maintainer/Claude Code runs above baseline and uploads reproducible evidence to #253 or a scoped draft PR.
2. Design team reviews three self-contained prototypes under #254. Choose one navigation/workspace architecture only after a short user pilot and safety review.
3. After G0 source audit, G1 user tasks and G2 design selection, authorize incremental #255 work focused on run→trace→replay→diff→evidence without any engine rewrite.

## Context, safety and provenance

This is a **source-based audit**, not a UI screenshot review or penetration test. Keep separate the older private strategic plans and current public evidence: old private plans have historically been corrected when newer code shipped. This file does not override security ADRs or grant permission to add unsafe executable operations.
