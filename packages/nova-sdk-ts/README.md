# @novafabric/sdk

> **Status: experimental** — implemented and tested, but the API surface may
> change without notice before 1.0. Versioning tracks the NovaFabric repo and
> the `/v0` REST contract it is generated against (ADR-0194).

Official TypeScript client for the NovaFabric `/v0` REST API. A thin,
generated-plus-handwritten client: request/response **types are generated**
from `api/openapi.yaml` (the single source of truth), the **runtime is
handwritten** on native `fetch` — Node >= 18 and modern browsers.

**Zero runtime dependencies.** `package.json` `dependencies` is empty by
design and asserted by test.

## What it covers

- `getCapsule(runId)` / `listCapsules({limit, cursor})` — run capsule read + list
- `iterateCapsules({limit})` — async iterator that lazily walks `next_cursor` pages
- `getAsset(id)` / `listAssets({limit, cursor, asset_type, status})` — registry read + list
- `submitScore(runId, score)` — submit an externally-computed evaluation into
  the capsule's append-only `scores.jsonl` (`POST /capsules/{run_id}/scores`,
  ADR-0119). Typed request/response; a `200` idempotent replay returns
  `data: null` (inspect `meta.status` to distinguish it from a `201`)
- `exportEvidence(request)` / `getEvidenceBundle(bundleId)` /
  `downloadEvidenceBundle(bundleId)` — build a signed Evidence Bundle ZIP
  (`POST /evidence`, `202`), poll its metadata (`GET /evidence/{bundle_id}`),
  and fetch the ZIP as a `Uint8Array` (`GET /evidence/{bundle_id}/download`).
  Typed against `EvidenceExportRequest` / `BundleSummary`
- `otlpTraceEndpoint()` — configuration helper (ADR-0177): returns
  `{ url, headers }` for pointing an existing OTel JS exporter at the
  deployment's OTLP ingest (`/api/otlp/v1/traces`, on the serve surface — NOT
  under `/v0`). It does NOT encode or send OTLP — **you bring your own exporter**
- Auth: static bearer token **or** an async token-provider callback
  (`() => Promise<string>`) so your app owns refresh against its IdP
- Typed `NovaFabricApiError` mapped from the standard error envelope
  (`{"error": {"code", "message", "details?"}}`)
- RFC 9745 `Deprecation` / RFC 8594 `Sunset` header surfacing (ADR-0188):
  exposed on every result's `meta` and warned via `console.warn` once per
  process per endpoint

## What it deliberately does NOT do

- **No agent framework** — no orchestration, prompt management, run wrappers,
  or capture; NovaFabric explicitly does not compete with agent frameworks
  (the private `design/strategy/non-goals.md`)
- **No OIDC flows** — identity acquisition belongs to the host application;
  the SDK only attaches the bearer token you give it
- **No telemetry** — no version checks, no analytics, no network calls other
  than the API requests you invoke (asserted by test)
- **No default base URL** — private deployments are the norm; the constructor
  requires `baseUrl` and omitting it is a compile-time and runtime error

The `/v0` surface the client covers is capsules, assets, scores, and evidence
bundles; further server surfaces are added as they stabilize.

## Module formats

The package ships **dual ESM + CommonJS** builds from one TypeScript source,
with per-condition type declarations so both resolution modes get
correctly-flavoured `.d.ts`:

| Consumer | Entry | Types |
|---|---|---|
| `import` (ESM) | `dist/index.js` | `dist/index.d.ts` |
| `require` (CJS) | `dist/cjs/index.js` | `dist/cjs/index.d.ts` |

Still zero runtime dependencies and no bundler: the CJS half is a second
`tsc` pass plus a nested `dist/cjs/package.json` declaring
`{"type":"commonjs"}`, which scopes the override away from the root
`"type": "module"`. A real `require()` of the built output is exercised by
`npm run test:cjs` and in CI — not merely asserted about the `exports` map,
since a test runner's own resolver can mask a genuine `require()` failure.

## Usage

```ts
import { NovaFabricClient, NovaFabricApiError } from "@novafabric/sdk";

const client = new NovaFabricClient({
  baseUrl: "https://nova.example.com/v0", // required — include the /v0 prefix
  token: async () => myIdp.getAccessToken(), // or a static string
});
```

Or from CommonJS:

```js
const { NovaFabricClient } = require("@novafabric/sdk");

const { data: capsule, meta } = await client.getCapsule("run-01H...");
if (meta.deprecation) {
  // RFC 9745 header, also console.warn'ed once per process per endpoint
}

for await (const summary of client.iterateCapsules({ limit: 100 })) {
  console.log(summary.run_id, summary.status);
}

try {
  await client.getAsset("00000000-0000-0000-0000-000000000000");
} catch (err) {
  if (err instanceof NovaFabricApiError) {
    console.error(err.status, err.code, err.message);
  }
}

// Submit an externally-computed score (append-only; ADR-0119).
const { data, meta } = await client.submitScore("run-01H...", {
  name: "faithfulness",
  value: 0.92,
  value_type: "numeric",
  source: "judge",
  evaluator_id: "eval-01H",
  subject: "sha256:...",
  subject_kind: "capsule",
  eval_card_digest: "sha256:...",
});
if (meta.status === 200) {
  // idempotent replay — identical body already present; data is null
}

// Build a signed Evidence Bundle, wait for it, then download the ZIP bytes.
const { data: bundle } = await client.exportEvidence({
  run_id: "run-01H...",
  allow_unsafe_skips: false,
});
const { data: zipBytes } = await client.downloadEvidenceBundle(bundle.bundle_id);
// zipBytes is a Uint8Array — write it to disk, or wrap it in a Blob in the browser.

// Point YOUR OWN OTel exporter at the deployment's OTLP ingest (ADR-0177).
// The SDK returns the URL + auth headers; it does not encode/send OTLP.
const { url, headers } = await client.otlpTraceEndpoint();
// import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
// const exporter = new OTLPTraceExporter({ url, headers });
```

## Development

```bash
npm install
npm run lint          # tsc --noEmit (strict)
npm test              # vitest, mocked fetch — no network
npm run generate:types  # regenerate src/types.gen.ts from ../../api/openapi.yaml
npm run check:drift   # fails if types.gen.ts is out of sync with the YAML
npm run build         # emit dist/ (ESM + d.ts)
```

`src/types.gen.ts` is committed; `npm run check:drift` regenerates to a temp
file and diffs, so any change to `api/openapi.yaml` without a matching
regeneration fails the gate. Generation runs through
`scripts/generate-types.mjs` rather than the bare `openapi-typescript` CLI so
the spec can be loaded and generated programmatically; the script no longer
patches the document on the way through.

`api/openapi.yaml` is itself generated from the server app
(`scripts/gen_openapi.py`), so the chain is **server → spec → types** and the
server is authoritative. Response schemas are *declared* on the routes rather
than bound to them, which is why the generated types name real shapes without
the server filtering any response body (`ADR-0227` — see the
[decision ledger](../../docs/decisions.md)).

Two types are derived from operations rather than from `components.schemas`:
`EvidenceExportRequest` and `ScoreSubmission` are request bodies, which the spec
describes inline on the operation, and `PaginationMeta`, whose fields the server
inherits into each list schema and therefore sends flat.

These gates run in CI via `.github/workflows/sdk-ts.yml`, path-scoped to
`packages/nova-sdk-ts/**` and `api/openapi.yaml` (so a contract change that
skips regeneration trips the drift gate). The workflow does **not** publish —
publication to npm is manual and happens from the public repo only, in lockstep
with the release tags (ADR-0194 D5).

## License

Apache-2.0 — see [LICENSE](./LICENSE).
