# Golden fixtures — saved views (ADR-0130 / saved-views-v0)

**Status:** future design (proposed). Private draft fixtures; graduate to `/tests/fixtures/` on
acceptance. One schema:
[`saved-view.schema.json`](../../../../schemas/saved-view.schema.json) — the saved-view **envelope** that
wraps an ADR-0129 query object. The nested `query` is kept opaque (`type: object`, carried by
reference) so the envelope stays valid regardless of query-grammar evolution; a saved view adds
persistence and ergonomics, not query power.

Last verified with `jsonschema` (MIT) + format checker: **11/11 behave as expected.** YAML is the
default on-disk form and JSON is equally valid — the fixtures are JSON; the field set is identical.

| Fixture | Expected | Exercises |
|---|---|---|
| `valid-full.json` | valid | all fields incl. `display` (columns/sort/format) + `tags` |
| `valid-side-effect.json` | valid | `created_by: null`, a `contains` query, no display |
| `valid-minimal.json` | valid | required-only view, `query: {from: capsules}` |
| `valid-null-optionals.json` | valid | `description`/`created_by`/`updated_at`/`display` all `null` |
| `invalid-bad-view-id.json` | reject | `view_id: "Failed Runs"` (not a slug) |
| `invalid-missing-query.json` | reject | required `query` absent |
| `invalid-query-not-object.json` | reject | `query` a string, not an object |
| `invalid-bad-format.json` | reject | `display.format` outside `{table,json,csv}` |
| `invalid-bad-sort-order.json` | reject | `display.sort[].order` outside `{asc,desc}` |
| `invalid-tags-nonstring.json` | reject | `tags` contains a number |
| `invalid-unknown-key.json` | reject | unknown top-level key (envelope closed) |

**Note:** `view_id` slug derivation, overwrite/`--force` semantics (`created_at` preserved),
`display`-vs-`--format` precedence (flag wins), corrupt-file isolation (non-blocking), and the
`nova view run` ≡ `nova query` equivalence (I2) are **behavioral** invariants verified by acceptance
tests, not JSON Schema. The `query` object's internal grammar is ADR-0129's, not this spec's.
