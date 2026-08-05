# Assurance Cases

An **assurance case** is a machine-checkable argument — in the GSN/SACM/CAE
tradition — that a system meets a goal, where every claim ultimately rests on
**sealed capsule evidence referenced by digest**. NovaFabric models the
argument graph, checks its structure, tracks whether its evidence is still
current, maps it onto named standards as *receipts*, and records challenges
(*defeaters*) — all offline
([ADR-0166](./decisions.md)).

**Status: experimental** (first slices of ADR-0166 D1–D6, shipped
2026-07-16). The honest scope note up front: ADR-0166's larger
**continuous-assurance loop is proposed design direction, not a shipped,
running system** — see [What remains future design](#what-remains-future-design).

Two commands share the `assure` prefix; they are different things:

| Command | What it is |
|---|---|
| `nova assure <capsule>` | OWASP LLM Top 10 (2025) evidence checks against a **single capsule** — an older, separate surface (exits 1 if any check fails) |
| `nova assure-case <document>` / `nova assure-coverage <document>` | The ADR-0166 assurance-case surface documented on this page |

---

## 1. The argument graph (D1)

Node types: `goal`, `strategy`, `solution`, `context`, `assumption`,
`justification`. `solution` leaves bind to sealed capsule roots via
`evidence_refs` — **`{ref, digest}` only, never clause bodies, findings text,
or PII**.

Structural invariants, enforced by validation:

- exactly one **top goal** (a `goal` node nothing else supports),
- the `supported_by` graph is **acyclic**,
- **no orphan** — every node is reachable from the top goal,
- ids unique, every `supported_by` reference resolves.

A `solution` node whose evidence doesn't resolve is reported as an
`unsupported_leaf` — recorded, **not fatal** — so an in-progress argument
stays valid while honestly flagging its gaps.

## 2. Checking a case: nova assure-case

The document is a JSON bundle: `case` (required — `{case_id, nodes[]}`) plus
optional `resolvable_digests` (evidence digests that currently resolve),
`currency` (a D2 ledger — requires `--as-of`), `conformance` (D3 clause
mappings), and `defeaters` (D4 challenges).

```bash
$ cat case.json
{"case": {"case_id": "acme-triage-v1", "nodes": [
   {"id": "G1", "type": "goal",
    "statement": "The support-triage agent operates within its approved policy envelope.",
    "supported_by": ["S1"]},
   {"id": "S1", "type": "strategy",
    "statement": "Argue over sealed run evidence from the July eval window.",
    "supported_by": ["E1", "E2"]},
   {"id": "E1", "type": "solution",
    "statement": "Sealed eval-suite capsule shows the policy gate passing.",
    "evidence_refs": [{"ref": "capsule:run-2041", "digest": "a3f1c2d4…abcd"}]},
   {"id": "E2", "type": "solution",
    "statement": "Sealed redaction report for the same window.",
    "evidence_refs": [{"ref": "capsule:run-2042", "digest": "b4e2d3c5…bcde"}]}]},
 "resolvable_digests": ["a3f1c2d4…abcd"]}

$ nova assure-case case.json
Assurance case: acme-triage-v1  (4 nodes)
Structure: VALID   top goal: G1
  unsupported leaf: E2
```

Exit codes: `0` — case OK; `1` — structurally invalid **or** any defeater is
open (the argument is defeated); `2` — bad input. `--json` emits the full
machine-readable report.

**The `--as-of` rule.** If the document carries a currency ledger, the
command refuses to run without an explicit `--as-of <ISO-8601>` — currency is
**never inferred from the system clock**. Evaluate at a sealed timestamp so
the result is a pure, reproducible function of the ledger plus that time.

## 3. Currency — arguments go stale (D2)

Each `solution` node's evidence has a validity window (`last_refreshed` +
`evidence_window`). At the supplied `--as-of` instant, each node is
`current`, `due` (inside the due-lead before expiry — refresh soon), or
`overdue` (no longer current). Overdue nodes yield `stale` drift records
(reason `evidence_expired`) — the same drift-record shape the
[drift detection subsystem](drift-gate.md) uses, so staleness is recorded
evidence, not a silent re-decision.

## 4. Conformance receipts — never verdicts (D3)

A `conformance` map binds `goal`/`strategy` nodes to named external standard
clauses (supported identifiers today: `iso_iec_42001`, `iso_iec_42005`,
`ul_4600`, `eu_ai_act`, `nist_ai_rmf`, `iso_iec_ieee_15026`). The output is a
**conformance-receipt** shaped like an OSCAL assessment-results document:
each mapped node becomes an `observation`; a mapping to an absent node, or to
an unsupported evidence leaf, becomes a `gap`.

This is deliberately **not a certificate**: it records that an argument was
*assembled against* a clause — never that the clause is *met*. NovaFabric
produces evidence that supports compliance workflows; it does not certify
compliance with any regulation.

## 5. Defeaters — recorded challenges (D4)

A **defeater** is a recorded challenge to a node: a *rebuttal* (the claim is
false) or an *undercut* (the inference doesn't hold). While `open`, it
undermines its target and `nova assure-case` exits `1` — the argument is
honestly *defeated at that node*. Clearing requires `withdrawn` or
`rebutted`, and a rebuttal **must** bind the evidence that answers it
(`resolved_by`) — you cannot declare a challenge answered without saying
what answered it. Open defeaters also emit `defeater_open` drift records.

## 6. Coverage — counts and gaps, never a grade: nova assure-coverage

```bash
nova assure-coverage case.json
nova assure-coverage case.json --as-of 2026-06-01T00:00:00Z --json
```

Reports the **structural** coverage of the argument: `total_goals`,
`goals_with_resolvable_leaf`, `unsupported_leaves`, `open_defeaters`,
`overdue_nodes`. Per the ADR there is deliberately **no grade, score, or
pass/fail field** — an "assurance score" would read as a verdict. The command
exits `0` whenever it renders: open defeaters and unsupported leaves are
coverage *facts* here (contrast `assure-case`, which treats an open defeater
as a failing state).

## 7. What remains future design

Per the ADR-0166 status record, be precise about what you can rely on:

- **Shipped experimental:** the D1 graph model + validation, the D2 currency
  ledger (explicit `--as-of` only), the D3 conformance receipt, the D4
  defeater model and coverage, the `nova assure-case` / `nova assure-coverage`
  read-side CLIs, and the D5 assessor-package **models** (package
  composition, deterministic digest, renewal delta — Python API only).
- **Future design (not implemented):**
  - sealing the assessor package through the Evidence-Bundle DSSE+timestamp
    path, and the `nova assure-case package` / `renewal` CLI surface;
  - the **continuous-assurance loop** itself — the ADR's vision of drift
    detection continuously feeding argument currency (NF-151..160,
    ADR-0147) is *proposed*, not shipped or running; today you re-run
    `assure-case` yourself against sealed timestamps;
  - any automated evidence collection — evidence digests, currency
    timestamps, and defeaters are all supplied in the document by you.

Do not represent an assurance-case report as a certification artifact; it is
a machine-checked argument over evidence you assembled.

---

## See also

- [Trust surfaces](trust-surfaces.md) — the verification projections whose
  outputs feed `solution` evidence
- [Drift detection](drift-gate.md) — the drift-record model reused for
  staleness and defeaters
- [User guide — trust layer](user-guide.md) — sealing and evidence bundles
- `nova assure-case --help`, `nova assure-coverage --help` — exact document
  schemas
