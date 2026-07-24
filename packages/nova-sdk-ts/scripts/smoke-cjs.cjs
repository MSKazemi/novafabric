/**
 * CommonJS consumption smoke test (ADR-0194 D5).
 *
 * Deliberately a real `require()` of the BUILT output, run by Node itself —
 * not a vitest assertion about the exports map. Vitest resolves through its
 * own bundler, so it would happily pass while a genuine `require()` failed
 * on the `"type": "module"` boundary. This file is `.cjs` so it is CommonJS
 * regardless of the root package.json.
 *
 * Run via `npm run test:cjs` (builds first) and in the sdk-ts CI lane.
 */
const assert = require("node:assert/strict");
const path = require("node:path");

const distEntry = path.join(__dirname, "..", "dist", "cjs", "index.js");

// 1. The package resolves through the `require` condition of exports.
const sdk = require(distEntry);

// 2. The public surface is actually present (not an empty interop object).
assert.equal(
  typeof sdk.NovaFabricClient,
  "function",
  "NovaFabricClient missing from the CommonJS build",
);
assert.equal(
  typeof sdk.NovaFabricApiError,
  "function",
  "NovaFabricApiError missing from the CommonJS build",
);
assert.equal(
  typeof sdk.resetDeprecationWarnings,
  "function",
  "resetDeprecationWarnings missing from the CommonJS build",
);

// 3. The nested marker that makes the whole thing work is in place.
const cjsPkg = require(path.join(__dirname, "..", "dist", "cjs", "package.json"));
assert.equal(
  cjsPkg.type,
  "commonjs",
  'dist/cjs/package.json must declare {"type":"commonjs"}',
);

// 4. It does not merely import — it runs. A stub fetch keeps this offline
//    (ADR-0194 D4: the SDK makes no request that the user did not invoke).
let observedUrl = null;
const client = new sdk.NovaFabricClient({
  baseUrl: "https://nova.example.com/v0",
  token: "smoke-token",
  fetch: async (url) => {
    observedUrl = String(url);
    return new Response(
      JSON.stringify({ items: [], next_cursor: null, total: 0 }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  },
});

client
  .listCapsules()
  .then((result) => {
    assert.ok(observedUrl?.includes("/capsules"), "listCapsules did not call /capsules");
    assert.deepEqual(result.data.items, []);
    console.log("[sdk] CommonJS smoke test passed");
  })
  .catch((error) => {
    console.error("[sdk] CommonJS smoke test FAILED:", error);
    process.exit(1);
  });
