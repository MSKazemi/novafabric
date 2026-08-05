# Golden fixtures — dataset-experiment harness (ADR-0120 / dataset-experiment-v0)

**Status:** experimental (first slice shipped 2026-07-15). These golden fixtures are exercised in CI
by `tests/eval/test_experiment_record.py` against the **graduated** `/schemas/` copies (identity
metadata changed only). Two schemas:
[`experiment.schema.json`](../../../../schemas/experiment.schema.json) (the immutable per-item
`Experiment` record) and
[`experiment-comparison.schema.json`](../../../../schemas/experiment-comparison.schema.json) (the
`nova experiment compare` result, which embeds an ADR-0080 significance verdict verbatim).

Last verified with `jsonschema` (MIT) + format checker: **14/14 behave as expected.** Fixtures
carry full `sha256:<64hex>` digests (real SHA-256 of a canonical string) and valid 26-char ULIDs,
so they exercise the actual `pattern` constraints.

| Fixture | Schema | Expected | Exercises |
|---|---|---|---|
| `experiment-valid-finalized.json` | experiment | valid | finalized, boolean metric with Wilson band, 2 items |
| `experiment-valid-running.json` | experiment | valid | `status: running`, empty `aggregate`, item with no scores |
| `experiment-valid-numeric-metric.json` | experiment | valid | numeric metric (`reducer: mean`, `wilson: null`), `agent` target |
| `experiment-invalid-bad-experiment-id.json` | experiment | reject | `experiment_id` not a ULID |
| `experiment-invalid-bad-status.json` | experiment | reject | `status` outside `{running,finalized}` |
| `experiment-invalid-bad-dataset-hash.json` | experiment | reject | `dataset_ref.dataset_hash` not `sha256:<64hex>` |
| `experiment-invalid-missing-target.json` | experiment | reject | required `target` absent |
| `experiment-invalid-bad-value-type.json` | experiment | reject | `aggregate[].value_type` outside `{boolean,numeric}` |
| `experiment-invalid-unknown-key.json` | experiment | reject | unknown top-level key (closed schema) |
| `comparison-valid.json` | comparison | valid | per-item alignment + `CONTINUE` verdict, `exit_code: 0` |
| `comparison-valid-regression-exit3.json` | comparison | valid | `ACCEPT_H1` regression, `exit_code: 3` |
| `comparison-invalid-bad-exit-code.json` | comparison | reject | `exit_code` outside `{0,3}` |
| `comparison-invalid-bad-baseline-id.json` | comparison | reject | `comparison_of.baseline_experiment_id` not a ULID |
| `comparison-invalid-unknown-key.json` | comparison | reject | unknown top-level key |

**Note:** the immutability of a finalized record, the requirement that `compare` refuse a
`dataset_ref` mismatch, per-item `unmatched`/`error` exclusion from the SPRT sequences, and the
zero-token default are **behavioral** invariants verified by acceptance tests, not JSON Schema.
The `significance` block is an opaque object here by design — its shape is owned verbatim by
ADR-0080 `regression_diff`, not re-specified by this record.
