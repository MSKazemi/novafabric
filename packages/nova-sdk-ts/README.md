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
  (`design/strategy/non-goals.md`)
- **No OIDC flows** — identity acquisition belongs to the host application;
  the SDK only attaches the bearer token you give it
- **No telemetry** — no version checks, no analytics, no network calls other
  than the API requests you invoke (asserted by test)
- **No default base URL** — private deployments are the norm; the constructor
  requires `baseUrl` and omitting it is a compile-time and runtime error

Planned for a later slice (not in this package yet): score submission
(`POST /capsules/{run_id}/scores`), the OTLP-ingest configuration helper
(ADR-0177), evidence helpers, and CJS output (the package is currently
**ESM-only** with bundled `.d.ts`; CJS compatibility is an honest follow-up,
not silently claimed).

## Usage

```ts
import { NovaFabricClient, NovaFabricApiError } from "@novafabric/sdk";

const client = new NovaFabricClient({
  baseUrl: "https://nova.example.com/v0", // required — include the /v0 prefix
  token: async () => myIdp.getAccessToken(), // or a static string
});

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
`scripts/generate-types.mjs` rather than the bare `openapi-typescript` CLI
because of a pre-existing spec bug (two dangling `$ref`s on
`/admin/flush-jwks`); see the comment in that script.

CI wiring for these gates is a follow-up (ADR-0194 first-slice note); the
package is published to npm from the public repo only.

## License

Apache-2.0 — see [LICENSE](./LICENSE).
