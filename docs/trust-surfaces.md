# Trust Surfaces

Four read-only commands that turn a capsule's trust evidence into something a
human can read at a glance: the Merkle proof tree, the trust-attestation
radar, the redaction/secret-scan X-Ray, and the portable agent passport. All
four are **pure projections** — they read verification output NovaFabric
already produces, mutate nothing, change no schema, and add no dependency.

**Status: experimental** (v0.61, shipped 2026-07-16). Each is the data/CLI
half of a two-part feature; the interactive `web/` dashboard views the ADRs
describe (proof-tree explorer, SVG radar glyph, heat-overlay tree) are
**future design** and not implemented.

In this first slice, each command takes a small **JSON document you assemble**
from existing outputs (`nova verify`, seal metadata, masking findings) — the
collectors that read these directly from a capsule are documented follow-ons.

| Command | Shows | ADR |
|---|---|---|
| `nova merkle-tree` | Which sealed fields roll up to which root — the proof structure | [ADR-0172](../design/adr/0172-evidence-provenance-merkle-tree-visualization.md) |
| `nova trust-radar` | The seven verification guarantees as one shape + verdict | [ADR-0173](../design/adr/0173-trust-attestation-radar-visualization.md) |
| `nova redaction-xray` | Per-field protection state + a conservative coverage meter | [ADR-0174](../design/adr/0174-redaction-secret-scan-xray-visualization.md) |
| `nova passport` | A portable green/amber/red identity summary for an agent | [ADR-0149](../design/adr/0149-agent-standard-interop.md) |

All four honor the redaction invariant (ADR-0009) at the type level: they
carry field *paths*, hash *prefixes*, and *refs* — never field values,
finding bodies, or component contents.

---

## nova merkle-tree (ADR-0172)

Renders the Evidence Provenance Merkle proof tree from a sealed capsule's
leaf hashes — `leaf → intermediate → seal-root → tsr`. The layers are
enumerated by the *same* canonical pairing/padding code that computed the
sealed root, so the projected tree can never diverge from what was signed. A
supplied `sealed_root` is compared against the recomputed root: match ⇒
verified, mismatch ⇒ flagged.

Input: a JSON object with `leaf_hashes` (required) plus optional
`leaf_labels` (field paths, never values), `sealed_root`, and `tsr_hash`.

```bash
$ nova merkle-tree tree.json --capsule-id run-2041
Merkle proof tree: sealed   capsule: run-2041
  ✓ leaf         d2dbf0..6c8188 (verified)  metadata.command
  ✓ leaf         4140bf..00e855 (verified)  env_lock.python
  ✓ leaf         649837..fe0f9a (verified)  events[0].digest
  ✓ leaf         9fde56..502454 (verified)  outputs.stdout_sha256
    ✓ intermediate c76c13..8d9831 (verified)
    ✓ intermediate 6d001d..2254bf (verified)
      ✓ seal-root    1313c9..66e0be (verified)
```

`--json` emits the `ProofTree` model instead. Exit codes: `0` — rendered
(sealed+verified, or unsealed); `1` — seal-root mismatch (tamper); `2` —
missing/malformed input. Without a `sealed_root` the tree renders `unsealed`
with every node `unverified`. A `tsr_hash` node renders as `unverified` in
v0 — RFC 3161 verification is out of scope for this projection.

## nova trust-radar (ADR-0173)

Projects a capsule's seven verification guarantees onto a fixed-axis radar —
`signature · timestamp · log_integrity · redaction_coverage · secret_scan ·
policy · eval_gate` — in a fixed order so shapes are comparable across
capsules. Booleans plot as 0/1; `redaction_coverage` is a clamped ratio; an
absent/`null` guarantee becomes an **`n/a` axis, kept distinct from a fail**
(an unsealed capsule has no `signature_ok`; that is not a failure).

Input: a JSON object of the guarantees, e.g. the flags from `nova verify`
output (`signature_ok`, `timestamp_ok`, `log_integrity_ok`,
`redaction_coverage`, `secret_scan_clean`, `policy_pass`, `eval_gate_pass`).

```bash
$ nova trust-radar verify.json --capsule-id run-2041
Trust radar: PARTIAL   capsule: run-2041
  ● Signature            1.00  (ok)
  ● Timestamp            1.00  (ok)
  ● Log integrity        1.00  (ok)
  ◐ Redaction coverage   0.97  (warn)
  ● Secret scan          1.00  (ok)
  ● Policy               1.00  (ok)
  · Eval gate             n/a  (na)
```

Verdicts: `attested` (sealed, every applicable axis ok), `partial` (sealed,
some axis short of 1), `critical` (a seal-integrity anchor — signature or
log-integrity — **failed**), `unsealed` (no signature guarantee; an unsealed
capsule can never be `attested`, however clean its policy and eval axes).
Exit codes: `0` for attested/partial/unsealed (informational), `1` only on
`critical`, `2` on bad input.

## nova redaction-xray (ADR-0174)

Projects a capsule's field-protection state — which fields are `clear`,
`redacted`, `secret_scrubbed`, `never_captured`, or `unknown` — with
per-state counts and a coverage meter. **No field value is ever printed**;
the report model has no value field at all, so a value handed in alongside a
record cannot reach the output.

Input: a JSON object with either `fields` (`[{path, state}]`) or raw
`findings` (MaskingPipeline finding records, adapted automatically).

```bash
$ nova redaction-xray xray.json --capsule-id run-2041
Redaction X-Ray: coverage 67% of sensitive surface   capsule: run-2041
  counts: clear=1  redacted=1  secret_scrubbed=1  never_captured=1  unknown=1
  ■ request.headers.authorization  (secret_scrubbed)
  ▨ request.body.messages[0].content  (redacted)
  · response.body.usage.total_tokens  (clear)
  ░ env.OPENAI_API_KEY  (never_captured)
  ? response.body.choices[0].text  (unknown)
```

Coverage is deliberately conservative:
`(redacted + secret_scrubbed) / (redacted + secret_scrubbed + unknown)` —
a field with absent scan metadata (`unknown`) *lowers* coverage instead of
being asserted clear. Exit codes: `0` rendered, `2` bad input.

## nova passport (ADR-0149)

Projects the identity, lineage, AIBOM, eval-card, package, and delegation
references NovaFabric already produces for an agent into one portable
passport document, verifiable offline:

- **green** — every component present and resolvable (carried as a ref);
- **amber** — the identity anchor exists but a component is absent or
  **opaque** (present but unattestable, e.g. an opaque lineage ancestor);
- **red** — the identity anchor is absent: no basis for a passport.

The passport never claims ancestry NovaFabric cannot attest — an opaque
ancestor is `amber`, never dressed up as `green`. Components carry a
ref/digest only, never the component body.

Input: `{"agent_ref": ..., "present": {component: ref}, "opaque": [...]}`.

```bash
$ nova passport issue refs.json
agent: agent:support-triage@1.4.0
status: (amber)
  identity    present  sha256:2f4a1c
  lineage     opaque   —
  aibom       present  sha256:77aa0b
  card        present  sha256:e01d9f
  package     present  sha256:5b2c44
  delegation  present  sha256:0d3e21

$ nova passport issue refs.json --json > agent-passport.json
$ nova passport verify agent-passport.json
...
verified — status (amber) matches
```

`verify` re-derives the verdict offline and confirms it matches the
document. Exit codes: `issue` — `0` rendered, `2` malformed input; `verify` —
`0` match, `3` status mismatch, `2` malformed input.

**Honest limits:** this slice emits an **unsigned** projection
(`signed: false`). Sealing the passport through the shipped seal path,
loading component refs directly from a sealed capsule (`--asset`), and the
broader ADR-0149 interop facets (NF-171..178, NF-180) are **future design**.

---

## See also

- [User guide — trust layer](user-guide.md) — `nova verify`, `nova redact`,
  `nova export-evidence`, the sources these projections consume
- [NovaSeal configuration](novaseal-configuration.md) — sealing, Merkle log,
  timestamps
- [Assurance cases](assurance-cases.md) — binding sealed evidence into a
  machine-checkable argument
