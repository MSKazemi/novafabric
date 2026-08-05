# Golden fixtures — pluggable PII masking pipeline (ADR-0135 / pii-masking-pipeline-v0)

**Status:** future design (proposed). Private draft fixtures; graduate to `/tests/fixtures/` on
acceptance. Three schemas:
[`masking-config.schema.json`](../../../../schemas/masking-config.schema.json) (`.novafabric/masking.yaml`),
[`masker-finding.schema.json`](../../../../schemas/masker-finding.schema.json) (a `masker_findings[]` proof
entry) and [`masker-error.schema.json`](../../../../schemas/masker-error.schema.json) (a `masker_errors[]`
proof entry). The pipeline is additive, opt-in, offline, Tier-A, layered **after** the built-in
ADR-0009 scanners; it extends `redaction-proof.json` with two arrays and never stores the raw value.

Last verified with `jsonschema` (MIT) + format checker: **14/14 behave as expected.**

| Fixture | Schema | Expected | Exercises |
|---|---|---|---|
| `config-valid-full.json` | config | valid | `enabled: true`, one masker with all knobs + `config` |
| `config-valid-minimal.json` | config | valid | one masker, `id` only |
| `config-valid-disabled.json` | config | valid | `enabled: false`, empty `maskers` |
| `config-invalid-masker-missing-id.json` | config | reject | masker without required `id` |
| `config-invalid-bad-on-error.json` | config | reject | `on_error` outside `{redact,drop}` |
| `config-invalid-timeout-zero.json` | config | reject | `timeout_ms: 0` (must be > 0) |
| `finding-valid.json` | finding | valid | applied custom mask with `match_hash` + `replacement` |
| `finding-invalid-bad-strategy.json` | finding | reject | `redaction_strategy` outside `{mask,hash,drop}` |
| `finding-invalid-bad-match-hash.json` | finding | reject | `match_hash` not `sha256:<64hex>` |
| `finding-invalid-missing-finding-id.json` | finding | reject | required `finding_id` absent |
| `finding-invalid-bad-finding-id.json` | finding | reject | `finding_id` not a ULID |
| `error-valid.json` | error | valid | fail-closed timeout entry with `detail_hash` |
| `error-invalid-bad-reason.json` | error | reject | `reason` outside the enum |
| `error-invalid-bad-action.json` | error | reject | `action_taken` outside `{redact,drop}` |

**Note:** masker purity/determinism/offline, run-after-built-ins ordering, before-persistence
masking, bounded/fail-closed execution, workload non-blocking, `UNCHANGED` no-op suppression, and
the `chain_hash` inclusion of the new arrays are **normative behavioral** invariants verified by
acceptance tests, not JSON Schema. The raw value is never stored — only `sha256:` hashes and markers.
