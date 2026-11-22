# NovaFabric Console 2.0 — Claude Code implementation runbook

**Status:** actionable handoff for an **existing** programme, not evidence of completed code. **Updated:** 2026-11-22.
**Canonical programme:** [#252](https://github.com/MSKazemi/novafabric/issues/252).
**Workstreams:** [W1 #275](https://github.com/MSKazemi/novafabric/issues/275) UX; [W2 #272](https://github.com/MSKazemi/novafabric/issues/272) architecture; [W3 #277](https://github.com/MSKazemi/novafabric/issues/277) Developer Studio; [W4 #279](https://github.com/MSKazemi/novafabric/issues/279) evidence; [W5 #281](https://github.com/MSKazemi/novafabric/issues/281) operations; [W6 #284](https://github.com/MSKazemi/novafabric/issues/284) quality. Project board: the maintainer's existing board (not linked here), governance [#250](https://github.com/MSKazemi/novafabric/issues/250).

## How to use

Open **Claude Code inside a checkout of the public `MSKazemi/novafabric` repository**, authenticated for the GitHub operations you actually want. Paste the single master prompt below. Run it with the strongest available **reasoning-oriented coding model** for code architecture/security decisions. After the architecture ADR and design decision, use an effective coding model for individual bounded issue PRs. Model version/effort is not a substitute for tests. The prompt is designed to run **one safe work packet at a time**, not to refactor everything in a single PR.

A separate agent operating the **private** repository may read its design documents, but private docs, raw capsules, confidential comparison or strategic analysis must **not** be copied into the public repository or PR body.

## Canonical implementation ownership

| Phase | Owning epic | Existing issues | Decision/gate |
|---|---|---|---|
| D0 baseline and user journeys | #275 (W1) | #253, #254 | real local browser/test audit; prototype evaluation |
| D1 architecture/security and design foundations | #272 (W2) | #262, #263, #267, #283; W1 navigation #266; W6 security #278 | **accepted** ADR, API contract, security and UX design |
| D2 first meaningful workflow | #277 (W3) | #255, #268, #269, #270; W1 onboarding #265 | source-linked Run→Trace→Replay/Diff, backed by real data |
| D2/D3 independent proof + topology | #279 (W4) | #271, #274; shared #256 | true verifier dimensions, portable bundle, bounded graph |
| D4 operational incident path | #281 (W5) | #276; shared #256 | read-only real alert→run→event→evidence |
| Cross-cutting release and performance | #284 (W6) | #261, #264, #278, #280, #282 | browser, accessibility, security, packaging and rollback |
| Future only, not a blocker | #252 (umbrella) | #285 P3 research | opt-in source-grounded assistance, NO default external model |

**Do not duplicate** the three existing research prototypes or the existing 14 task issues. This Markdown mapping is **not** GitHub native parent/blocked-by linkage; [#250](https://github.com/MSKazemi/novafabric/issues/250) owns the read-before-write project-board reconciliation.

## Exact implementation path and decision gates

### Gate 0 — source audit (may start immediately)
1. Read `AGENTS.md`, `CONTRIBUTING.md`, current `main`, all Console epics and in-flight PRs #251/#258/#259 and the current source. Inventory `ui/dashboard/`, `packages/nova-dashboard/`, `src/novafabric/serve/`, actual OpenAPI, CLI registry, authz, test + wheel packaging.
2. Record current HEAD, supported capabilities, 30 frozen tab IDs and URL history, route mapping, source/derived/unknown proof states. Mark all claims **OBSERVED / TARGET / NOT_RUN / UNSUPPORTED**; do not take a plan as a shipped feature.
3. Finish #253 missing real dashboard screenshots/perf/a11y and #254 comparable prototype tasks when browsers/test participants are available. No test participants → mark human validation NOT_RUN; do not invent data or mark #254 done.
4. Confirm all existing issues/branches/PRs, and pick the smallest next safe *implementation* slice. Avoid reimplementation of existing features.

### Gate 1 — architecture and security (decision before production shell migration)
- Issue #262: compare A `Astro 7 + React 19` shell evolution vs B `React + Vite` static shell; option C is a coherent Console↔Topology interaction using the existing Vite/Sigma/Three app and either A/B. Build **equal read-only** Run→Event→Proof thin spikes, no autonomous backend mutations. Compare code reuse, wheel bytes, startup, UX, browser memory, a11y, security, no Node at runtime, offline assets and migration blast radius.
- Issue #263: define Python wheel version vs Console UX-generation vs UI static asset SHA/digest vs API capability negotiation vs portable capsule/evidence schema. A frontend redesign does not bump the Python major version or cryptographic schema.
- Issue #278: threat-model existing `localStorage`, `?token=` topology links and SSE query auth, origin/referrer, clipboard/history, XSS, CSP, multi-tenant caches and permission revocation. Observed token patterns alone are NOT proof of exploitable vulnerability. Any auth/API change needs review/RFC and tests.
- **STOP when the selected ADR, security design or product concept needs human approval.** An unreviewed draft PR is not acceptance. Continue only with independent read-only measurements/test improvements; do not silently choose a framework or migrate production.

### Gate 2 — foundation (after ADR accepted)
- Issue #267: audit/reuse existing `tokens.css`, components, bundled fonts, Lucide, Tailwind. Build semantic status tokens, responsive dark/light and compact/comfortable density, keyboard primitives, proof status cards, accessible list/dialog/empty/error/denied states. Introduce new UI dependencies (React Aria/Radix/TanStack Table/Query) **only with a measured gap, license and bundle review**.
- Issue #266: five destinations **Overview / Investigate / Evidence & Trust / Operations / Advanced Tools**, with Developer/Evidence/Operations as **presentation presets, not authorization roles**. Preserve all 30 legacy tab URLs `?tab=`, shortcuts, saved filters, deep links and CLI equivalents; any route migration needs an explicit tested adapter.
- Issue #283: capability-aware unsupported/disabled/denied/loading/offline/unknown states and backend/UI version diagnostics. Do not invent API endpoints without separate approval.

### Gate 3 — first end-to-end **working** user journey
**Canonical first implementation ticket: [#255](https://github.com/MSKazemi/novafabric/issues/255), owned by W3 #277**, coordinated with #268/#269/#270 and W4 #271. Implement progressively in focused PRs, not one rewrite.

1. Select or deep-link a **real locally captured Run** (also support explicitly labeled synthetic fixtures) and preserve selected run/event ID across navigation.
2. Render source-linked Run Explorer→Trace and first **recorded** failed event with exact event provenance, timestamp, redaction/completeness; missing observations shown as missing rather than inferred cause.
3. Show an understandable *read-only* replay-mode explanation and actual capability/permission; differentiate forensic inspection, mocked replay and any permitted live re-execution. **Never auto-execute** or toast success after a denied backend result.
4. Select a second run, show server-authoritative aligned diff and first actual divergence or explicit “not determinable”; a changed tool/model step must never render as identical.
5. Navigate to per-check evidence status: content integrity, signature validity, signer trust, timestamp, inclusion/redaction/completeness **only when actual backend contracts support each**. Offline independent `nova verify` flow is first class; no global green Trusted or compliance certification promise.
6. Make every key path keyboard-operable and functional at 390/768/1024/1440; visibly separate source-recorded, replay-generated, derived, hypothetical and fixture content.
7. Test full journey on real `nova serve` with a bounded synthetic/golden capsule, a tampered and partial capsule, a signed-but-untrusted signer case, unavailable mode, denied action and old `?tab=` links.

Do not claim this gate complete based on sample HTML prototype or TestClient alone.

### Gate 4 — next releases (separate reviewed issue PRs)
- **W4 #279:** #271 independent verifier and explainable Trust Center, then #274 2D source-bounded semantic-zoom lineage/topology. Reuse React Flow for moderate DAGs and Sigma/Graphology where justified for dense graphs; 3D opt-in, lazy, accessible table alternative, no implied causality.
- **W5 #281:** #276 read-only alert→affected run→event→evidence from actual API correlation and time window, 401/403 and tenant isolation, source freshness and explicit missing data.
- **W6 #284:** #261 real browser scale gate (100K indexed runs, long trace, 10K graph synthetic baseline; 1M only opt-in, resource bounded), #282 memory-bounded incremental queries/stream stale/disconnect handling, #280 actual dashboard keyboard/axe/screen-reader, #264 wheel/static/cached-asset migration and rollback.
- **P3 #285** is research-only, not required for any release. An optional assistant cannot invent/certify evidence, leak prompts or auto-execute tools.

### Global definition of done / stop conditions

**Safety invariants:**
- Run Capsule and Evidence Bundle remain the only two top-level portable artifacts; `nova verify` works offline, independent of original server/network/db.
- No secret or unredacted prompt in browser URLs, logs, query state, screenshots, test traces, cached data or public PR. Full prompt capture is opt-in.
- No telemetry, update beacon, external font/CDN or mandatory SaaS. Local mode remains fully offline and SQLite-supported; no mandatory production Node runtime.
- Any privileged mutation remains backend authorized with scope preview and explicit confirmation. Fail closed on unverifiable proof, unsupported capabilities, role revocation and API refusal.
- No invented causality, false "identical" diff, blanket "Trusted" proof, fake count on non-computable totals, or success toast for `ok:false`.
- Preserve existing CLI/API semantics and route compatibility. Backend/schema/security modifications require their own approved RFC/ADR.

**Measured gates:** for each scoped PR, record baseline vs proposed with pinned Git SHA, exact commands, test environment and screenshots; p50/p95 and p99 only where sample sizes support them; browser responsiveness/heap slope for long sessions, wheel/static size, keyboard/axe, no critical proof/status or auth regressions. Human task targets (NOT results): failed run <60s, compare <90s, verifier verdict <60s, ≥90% task completion in defined pilot, zero false proof PASS.

**Tests for changed paths** (read actual `package.json` and branch framework before invocation):
```bash
cd ui/dashboard
npm ci
npm run lint
npm run test:unit
npm run test:e2e
npm run test:live
npm run test:a11y
npm run test:visual
npm run check:licenses
npm run build:dashboard

# repository root when relevant
uv sync --all-extras
make test-fast
make lint
make typecheck
make check-links
```
For topology changes also run the current `packages/nova-dashboard` tests/build, license and wheel-packaging checks. A changed architecture must preserve or replace equivalent gates. Record **PASS / FAIL / NOT_RUN** with cause; do not suppress failures, pretend a test ran, or run a destructive full suite on production. Keep traces synthetic/sanitized.

**PR discipline:** one issue, one bounded branch and one DRAFT PR; link issue, code paths, tests, before/after screenshots, user-task evidence, security/compatibility review, rollback and next blocker. No auto-merge, deployment, tag/release, cloud spend, new project, or changes to other workstreams.

## Master Claude Code prompt — copy all of the following block

```text
Act as NovaFabric's staff frontend architect, principal UX engineer,
evidence-safety reviewer and disciplined Claude Code implementation lead.

REPOSITORY AND PROJECT
- Public engineering repo: MSKazemi/novafabric (this working checkout).
- Private design material exists outside this repository; never reference
  or copy it into public artifacts.
- Existing project board: maintainer-managed, not linked here.
- Master epic: https://github.com/MSKazemi/novafabric/issues/252
- Workstream epics: W1 #275; W2 #272; W3 #277; W4 #279;
  W5 #281; W6 #284. Project/native sync issue #250.
- Public runbook (review branch or after merge):
  docs/console-2-claude-code-implementation-runbook.md

DO NOT CREATE A NEW UX EPIC, PROJECT, REDUNDANT ROADMAP OR DUPLICATE ISSUES.

YOUR JOB
Implement a strong, polished, cohesive, trustworthy, accessible,
local-first Console 2.0 with Developer Studio, Evidence Workbench and
Operations Console as three navigation presets of one conceptual app.
Default outcome: find a real failed run, inspect the exact source event,
understand replay safety, compare two runs and independently verify
the evidence. Scale browser UX without hiding missing/partial data.
No framework choice, feature claim or "DONE" is valid without evidence.

FIRST: DISCOVER THE TRUTH
1. Read AGENTS.md, CONTRIBUTING.md, current main HEAD/CI/ADRs,
   existing #252 and W1-W6 epics/child issue bodies, and active PRs
   #251, #258, #259 and #262/263/278 architecture-security gates.
2. Audit actual code and tests: ui/dashboard/ Astro+React/Tailwind
   shell, Sidebar/DashboardApp/RunsTab/Trace/Replay/Diff/Evidence,
   api.ts, command registry, saved view and ?tab history;
   packages/nova-dashboard/ Vite/Sigma/Graphology/Three; FastAPI
   serve and authz, OpenAPI, static wheel and offline verification.
3. Record real code support as IMPLEMENTED, PARTIAL, ABSENT,
   EXPERIMENTAL or NOT VERIFIED; pin SHA; reconcile open work.
4. Find existing draft PR work and reuse it. Do not overwrite
   human edits or build duplicate panels/data stores.

EXECUTE IN PHASES, ONE BOUNDED ISSUE/PR AT A TIME
PHASE 0 — W1 #253/#254: run the actual local UI/browser baseline
and evaluate three existing sample UX prototypes with equivalent
user tasks. If users/browsers unavailable, report NOT_RUN.
PHASE 1 — W2 #262/#263 and W6 #278: compare current Astro
evolution vs React/Vite static shell using identical read-only
Run->Event->Proof tasks. Include topology integration, wheel/offline,
30-tab aliases, security/privacy, build/API version and rollback.
Produce an evidence-backed ADR and STOP until owner acceptance;
an unmerged draft does not count as approval.
PHASE 2 — after accepted ADR, W2 #267/#283 and W1 #266/#265:
incrementally build semantic UI tokens, accessible responsive
components, navigation/presets, legacy-route adapter, bounded
context, honest capability UX and guided local onboarding.
PHASE 3 — W3 #255/#268/#269/#270: create the FIRST REAL
end-to-end run->recorded failure event->replay explanation->
source-authoritative diff->per-check evidence and CLI path.
Share only authorized stable IDs/query state between views.
Preserve local captures, deep links, and existing API/CLI meaning.
PHASE 4 — W4 #271/#274: evidence Trust Center + independent
offline verifier, then bounded 2D lineage with optional lazy
Sigma/3D and accessible relationship table.
PHASE 5 — W5 #276: actual alert->run->event->evidence with
backend-enforced tenant permissions and freshness.
PHASE 6 — W6 #261/#280/#282/#264 throughout: real browser
performance, bounded paging/memory, SSE reconnection truth,
security/a11y/keyboard, clean offline wheel, cached asset/version
compatibility, old URLs and rollback.

IMPLEMENTATION AND TEST DISCIPLINE
- Before coding each child issue, verify its predecessors and exact
  acceptance, add a failing focused test, change only one coherent
  vertical slice, run frontend + relevant Python gates; open DRAFT PR.
- When #262 or token/auth RFC requires human approval, STOP the
  affected implementation. Independent tests/spikes may continue.
- Preserve TWO portable artifacts (Run Capsule/Evidence Bundle),
  independent offline verification, privacy/redaction, local SQLite,
  no telemetry/CDN/update checks/mandatory remote service.
- Backend always decides authorization, capabilities, replay safety
  and cryptographic truth. Never invent proof/causality/metrics,
  auto-execute tools or label invalid/partial evidence as trusted.
- Never put tokens/prompts in share links, URL state, browser logs,
  persistent cache, traces or test artifacts.
- Keep all 30 legacy ?tab IDs/filters/shortcuts/CLI equivalents
  functional via verified compatibility adapter, including back
  navigation; don't load large graphs/WebGL by default.
- Test real local server, valid/invalid/partial/signer-untrusted
  fixtures, denied/expired capability, cross-tenant scope, 100K
  synthetic runs, long trace and large graph under explicit budget.
- Run current npm lint/unit/e2e/live/a11y/visual/license/build,
  topology tests when affected, and make test-fast/lint/typecheck/
  check-links for Python/docs changes. Record PASS/FAIL/NOT_RUN.
- Use strong source-backed before/after screenshots, accessibility
  walkthrough, memory/network/bundle measurements and CLI parity.
- Maintain at most THREE active engineering workstreams. No single
  mega-PR; do not modify release/security policy without review.
- Update the owning child issue and matching W1-W6 epic with links
  to exact PR, tested SHA, results, risk and rollback.
- Native project-board parent/blocker/field work belongs to #250:
  inspect live objects, show dry-run diff, apply only authorized
  missing links and verify. Issue-body links aren't native parents.

STOP / REPORT
At each human decision gate, show READY / BLOCKED / NOT_RUN and a
decision-ready ADR or review artifact. At the end of EACH scoped
issue, output: Reused work, changed files, test commands + actual
results, evidence/screenshots, security/accessibility/perf verdict,
draft PR/issue links, owner approvals still needed, and NEXT single
smallest unblocked issue.

NEVER merge, deploy, create a second Project, publish private info,
fabricate benchmarks, send telemetry, purchase resources or
automatically perform privileged production actions.
```

## First implementation task after design decision

In the current W3 [#255](https://github.com/MSKazemi/novafabric/issues/255), the first production PR should be a **minimal read-only Run→Event→Proof link** using existing APIs and selected shell, with URL-safe `selectedRunId` and `selectedEventId`, actual missing/denied/partial states, source provenance, old-route compatibility, keyboard access and no new backend execution. That slice creates the integration spine; replay/diff/Trust Center improvements follow in their owning issues after the capability and architecture decision.

**Not done by adding this runbook:** app redesign, UI tests, human pilot, architecture selection, native GitHub Project issue links, milestone creation, software release or production deployment.
