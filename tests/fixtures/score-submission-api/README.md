# Golden fixtures — external score submission API (ADR-0119 / score-submission-api-v0)

**Status:** experimental — graduated 2026-07-15 from `design/spec/fixtures/` on ADR-0119
implementation. Two schemas:
[`score-submission-request.schema.json`](../../../schemas/score-submission-request.schema.json) (the
POST body / SDK argument shape) and
[`score-submission-response.schema.json`](../../../schemas/score-submission-response.schema.json) (the
`201`/`200` body).

Verified in CI by `tests/eval/test_score_submission_schema.py` (`jsonschema` Draft 2020-12 +
format checker): **11/11 behave as their filename asserts.**

| Fixture | Schema | Expected | Exercises |
|---|---|---|---|
| `request-valid-full.json` | request | valid | all fields (`judge` source, `supersedes: null`) |
| `request-valid-minimal.json` | request | valid | required-only body (numeric/code) |
| `request-valid-supersedes.json` | request | valid | `supersedes` set to a prior `score_id` (ULID) |
| `request-invalid-missing-name.json` | request | reject | required `name` absent |
| `request-invalid-bad-value-type.json` | request | reject | `value_type` outside the enum |
| `request-invalid-bad-subject-hash.json` | request | reject | `subject` not `sha256:<64hex>` |
| `request-invalid-bad-source.json` | request | reject | `source` outside `{human,heuristic,code,judge}` |
| `request-invalid-unknown-key.json` | request | reject | unknown top-level key (closed schema) |
| `response-valid-201.json` | response | valid | fresh append + server `submission` block |
| `response-valid-200-idempotent.json` | response | valid | idempotent replay, no `submission` block |
| `response-invalid-missing-idempotent-replay.json` | response | reject | required `idempotent_replay` absent |

**Note:** the append-only guarantee, idempotent-replay semantics (Rule 6), and the requirement
that `supersedes` reference a `score_id` present in the *same* `scores.jsonl` (Rule 5) are
**behavioral** invariants verified by acceptance tests, not JSON Schema — a syntactically valid
`supersedes` ULID that points at no existing record is caught at ingest, not by the schema. The
server-only `submission` block is transport metadata and is never persisted into the capsule.
