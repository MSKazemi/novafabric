import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { NovaFabricClient } from "../src/index.js";
import { jsonResponse, recordingFetch } from "./helpers.js";

describe("package hygiene (ADR-0194)", () => {
  it("has zero runtime dependencies (D1: native fetch only)", () => {
    const pkg = JSON.parse(
      readFileSync(new URL("../package.json", import.meta.url), "utf8"),
    ) as Record<string, unknown>;

    expect(pkg).toHaveProperty("dependencies");
    expect(pkg["dependencies"]).toEqual({});
    expect(pkg["license"]).toBe("Apache-2.0");
    expect(pkg["name"]).toBe("@novafabric/sdk");
  });

  it("makes no request except user-invoked calls (D4: no telemetry)", async () => {
    const { fetchImpl, requests } = recordingFetch(() =>
      jsonResponse({ items: [], next_cursor: null, total: 0 }),
    );

    // Constructing the client must not touch the network...
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      token: "tok",
      fetch: fetchImpl,
    });
    // ...even after the event loop drains (no deferred pings/version checks).
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(requests).toHaveLength(0);

    // Exactly one request per user-invoked call — nothing extra.
    await client.listCapsules();
    expect(requests).toHaveLength(1);
    await client.listAssets();
    expect(requests).toHaveLength(2);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(requests).toHaveLength(2);
  });
});
