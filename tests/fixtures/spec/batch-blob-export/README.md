# Golden fixtures — batch capsule blob export (ADR-0141 / batch-blob-export-v0)

**Status:** future design (proposed). Private draft fixtures; graduate to `/tests/fixtures/` on
acceptance. One schema:
[`export-manifest.schema.json`](../../../../schemas/export-manifest.schema.json) — the signed
`export-manifest.json` **pointer manifest** for a batch of content-addressed capsule exports. The
manifest is written **last** and its presence is the atomic completion marker for the batch; it
lists members by `capsule_id` + `content_hash` + `size` (it does not repackage capsule bytes, unlike
an Evidence Bundle). Reuses the existing WORM storage adapters and CAS addressing unchanged.

Last verified with `jsonschema` (MIT) + format checker: **18/18 behave as expected.** The schema is
**closed** (`additionalProperties: false`) at the top level and on each member and the `worm` block —
arbitrary keys are permitted **only** inside `extensions`. The `batch_digest` in every valid fixture
is a **real** recomputed sha256 over the canonically sorted member list (sort by `content_hash`, ties
by `capsule_id`), so the fixtures double as determinism fixtures; the empty-batch digest is the
sha256 of the empty string (`e3b0c442…b855`).

| Fixture | Expected | Exercises |
|---|---|---|
| `valid-s3-three-members.json` | valid | 3-member S3 export, query-selected, WORM off, real batch_digest |
| `valid-local-worm-single.json` | valid | local-directory dest, `--worm compliance`, `query: null` |
| `valid-empty-batch.json` | valid | `count: 0`, `members: []`, digest over the empty set (legit attestation) |
| `valid-dedup-duplicate-content.json` | valid | two `capsule_id`s sharing one `content_hash` (dedup) + `extensions` |
| `invalid-missing-signature.json` | reject | required `signature` absent |
| `invalid-bad-schema-version.json` | reject | `schema_version` not `0.1.0` (pinned `const`) |
| `invalid-export-id-not-ulid.json` | reject | `export_id` free-form string, not a ULID |
| `invalid-export-id-non-crockford.json` | reject | 26-char id containing `L` (excluded from Crockford base32) |
| `invalid-batch-digest-too-short.json` | reject | `batch_digest` not `sha256:<64 hex>` |
| `invalid-member-bad-content-hash.json` | reject | member `content_hash` a truncated/elided hash |
| `invalid-member-negative-size.json` | reject | member `size` < 0 |
| `invalid-member-missing-size.json` | reject | member missing required `size` |
| `invalid-member-extra-field.json` | reject | member carries an unknown key (closed member) |
| `invalid-unknown-toplevel.json` | reject | unknown top-level key (closed schema) |
| `invalid-worm-bad-mode.json` | reject | `worm.mode` outside `{governance, compliance}` |
| `invalid-signature-empty-signatures.json` | reject | `signature.signatures` empty (`minItems: 1`) |
| `invalid-producer-bad-tool.json` | reject | `producer.tool` not `novafabric` (pinned `const`) |
| `invalid-bad-created-at.json` | reject | `created_at` not RFC 3339 |

**Note:** the normative export/verify **semantics** are behavioral, not schema-checkable:
`count == len(members)`; members written content-addressed and **idempotently** (existing blobs
skipped → resumable); manifest written **last** and signed only over a complete batch; `nova verify`
recomputing `batch_digest` + re-hashing every member offline → `VALID` / `INCOMPLETE` (member missing
at `dest`) / `INVALID` (bad signature or digest); WORM enforcement is destination-side (the manifest
records only intent); and export MUST NOT block or fail the user workload. These are covered by
acceptance tests, not by this JSON Schema.
