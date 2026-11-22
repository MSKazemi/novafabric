# Console 2.0 — Interactive UX concept prototypes (Issue #254)

**Status:** research artifact, not production UI. **Created:** 2026-11-22 (Europe/Rome).  
**Source:** `index.html` — a single dependency-free, self-contained HTML document with inline CSS/JS, sample data, three workspace layouts and responsive breakpoints.  
**No backend integration, no real authentication, no run capture/replay, no cryptographic verification, no signed exports, no external requests and no telemetry.** This folder is not part of the shipped `nova serve` dashboard static-asset build.

## Open the prototype

From repository root:

```bash
# Option 1: double-click the HTML file or open its file:// path
xdg-open ui/dashboard/prototypes/console-2/index.html

# Option 2: run a local static preview only (not NovaFabric server)
python3 -m http.server 8765 --directory ui/dashboard/prototypes/console-2
# Open http://127.0.0.1:8765/
```

No `npm install`, account, proxy, API key, browser extension or network access is required. A persistent **SYNTHETIC DATA** banner protects viewers from interpreting the UI as actual recorded evidence.

## Three design explorations

| Variant | Default palette | Primary tasks | Why distinct |
|---|---|---|---|
| **Developer Studio** | dark, high-information density, green accent | run list → trace/event inspector → replay modes → aligned diff | three-column investigation workspace; keyboard/search |
| **Evidence Workbench** | dark violet, documentation-focused | verification per check → lineage → export guidance | explicit validity vs signer trust vs timestamp vs redaction status |
| **Operations Console** | light blue/neutral, metric cards | metrics → alert triage → linked run/evidence → policy permissions | high-level signal to evidence without inventing source completeness |

All use one persistent top-level workspace selection and no per-variant service or account. Toggle theme globally. Visual properties are exploratory; the canonical production tokens remain `ui/dashboard/src/styles/tokens.css`.

## Implemented interactive demonstration behavior

- Choose Developer/Evidence/Operations using semantic buttons and selected states.
- Select workspace sub-navigation using keyboard or pointer.
- Developer: search the three sample runs; click a run to update timeline; click timeline events to update inspector; replay mode explainer (forensic, mocked, live unavailable) with step-through **presentation only**; compare example runs with changed and added events.
- Evidence: switch between content-intact/unanchored, incomplete and invalid-signature scenarios; select checks and inspect their distinct meanings; navigate the simple provenance graph and its equivalent relationship table; view local verifier command pattern and export checklist.
- Operations: view synthetic counts and trend, follow a sample alert to a source run or evidence view, inspect example backend capability vocabulary and safety notices.
- Responsive layout: three-column splits collapse at <=1100px and to a single column at <=760px; mobile panels and selections remain accessible.
- Accessible basics: button labels, aria-pressed/current on selection, focus-visible outline, Skip to main, accessible relationship table, reduced-motion preference. **Not yet independently audited** with a screen reader/axe/keyboard suite.

## Comparative heuristic rubric (score only after test)

| Measure | Developer | Evidence | Operations |
|---|---|---|---|
| First-use navigation clarity | to measure | to measure | to measure |
| Time to relevant source evidence | to measure | to measure | to measure |
| Replay-mode interpretation accuracy | to measure | to measure | to measure |
| Signature vs signer-trust comprehension | to measure | to measure | to measure |
| Failed-run/alert triage speed | to measure | to measure | to measure |
| Keyboard + screen-reader task completion | to measure | to measure | to measure |
| 390/768/1024/1440 screen usability | to measure | to measure | to measure |
| Integration cost and compatibility | to audit | to audit | to audit |

Do not convert design *intent* into measured success without actual participants and screen captures.

## Pilot walkthrough / task script (10–20 minutes each)

1. **T1:** Developer → find `RUN_C` using “failed”, click its failure event and describe which details were recorded vs inferred.
2. **T2:** Developer → Replay → choose forensic mode and explain why no code executes. Select mocked, then live, and explain what changes.
3. **T3:** Developer → Diff → identify the changed model response and added tool call. Explain whether comparison is server-authoritative.
4. **T4:** Evidence → Verification → choose “Content intact; signer unanchored”; distinguish integrity from signer trust. Switch to invalid signature and explain why no global “trusted” badge is appropriate.
5. **T5:** Evidence → Lineage → describe links using the accessible table. Navigate to Export and locate a local verifier command.
6. **T6:** Operations → Alerts → open “Tool execution failure” and follow its linked Run Explorer state.
7. **T7:** Operations → Governance → explain why a UI disabled button is not authorization.
8. **T8:** Complete at least one workflow without a mouse at 390px viewport, then at 1440px.

**Observe, do not coach:** successes, failures, time, confusion, unsafe interpretation of evidence and mode semantics, responsive usability, task completion, user preferences. Use one identical task corpus across variants and counterbalance presentation order.

## Known prototype limitations / design follow-up

- **Not a drop-in dashboard implementation.** It intentionally does not import NovaFabric's current React components, APIs, design tokens or CSS; migration must *reuse* existing architecture.
- Counts, run IDs, costs, statuses, chart bars, verifier outcomes and provenance edges are synthetic; no real run can be selected.
- Replay buttons only change explanatory text and selected recorded example events. They never reproduce execution.
- `nova verify` is an *illustrative command pattern* and must be generated from real export context in production.
- Theme and layout may require additional contrast review, especially at low zoom and dark/light variants.
- Browser/source live screenshots, real server interactions, a11y evaluation, and user tests are **not run**; keep issue #254 open.
- Prototype should not be added to the wheel, published to the production site or confused with an experimental backend.
- Historical `?tab=` IDs and shortcuts are preserved in production plans; this standalone prototype deliberately does not emulate all 30 tabs.
- Any production frontend change follows the approved branch/ADR/release-gate process; UI must not bypass backend security.

## Relevant work and approval flow

- [Program epic #252](https://github.com/MSKazemi/novafabric/issues/252)
- [Source/live audit #253](https://github.com/MSKazemi/novafabric/issues/253)
- [Prototype/user validation #254](https://github.com/MSKazemi/novafabric/issues/254)
- [Proposed implementation #255](https://github.com/MSKazemi/novafabric/issues/255)
- [Public modernization roadmap, draft PR #251](https://github.com/MSKazemi/novafabric/pull/251)

Next: run the live/source audit script and usability walkthrough; choose a design based on evidence. **Do not begin #255 until design and security acceptance.**
