/**
 * Compile-time-only assertions, enforced by `tsc --noEmit` (this file is in
 * the tsconfig include set but is never executed — vitest's default include
 * pattern does not match `*.test-d.ts`). If any @ts-expect-error below stops
 * erroring, tsc fails with "Unused '@ts-expect-error' directive".
 */
import { NovaFabricClient } from "../src/index.js";

// @ts-expect-error — constructor requires an options object (no default base URL, ADR-0194 D4)
new NovaFabricClient();

// @ts-expect-error — baseUrl is required
new NovaFabricClient({});

// @ts-expect-error — baseUrl is required even when a token is given
new NovaFabricClient({ token: "tok" });

// @ts-expect-error — baseUrl must be a string
new NovaFabricClient({ baseUrl: 42 });

// Valid constructions must continue to type-check:
new NovaFabricClient({ baseUrl: "https://nova.example.com/v0" });
new NovaFabricClient({
  baseUrl: "https://nova.example.com/v0",
  token: async () => "tok",
});
