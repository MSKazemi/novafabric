import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NovaFabricClient, resetDeprecationWarnings } from "../src/index.js";
import { jsonResponse, recordingFetch } from "./helpers.js";

const capsuleBody = {
  run_id: "run-1",
  status: "success",
  created_at: "2026-07-17T00:00:00Z",
};

const deprecatedHeaders = {
  deprecation: "@1735689600",
  sunset: "Sat, 01 Aug 2026 00:00:00 GMT",
};

describe("RFC 9745/8594 deprecation surfacing", () => {
  beforeEach(() => {
    resetDeprecationWarnings();
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("exposes Deprecation and Sunset headers on the response meta", async () => {
    const { fetchImpl } = recordingFetch([
      jsonResponse(capsuleBody, { headers: deprecatedHeaders }),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    const { meta } = await client.getCapsule("run-1");

    expect(meta.deprecation).toBe("@1735689600");
    expect(meta.sunset).toBe("Sat, 01 Aug 2026 00:00:00 GMT");
  });

  it("console.warns once per process per endpoint, not per call", async () => {
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse(capsuleBody, { headers: deprecatedHeaders }),
    );
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    await client.getCapsule("run-1");
    await client.getCapsule("run-2"); // same endpoint, different path param

    expect(console.warn).toHaveBeenCalledTimes(1);
    expect(vi.mocked(console.warn).mock.calls[0]?.[0]).toContain(
      "GET /capsules/{run_id}",
    );
    expect(vi.mocked(console.warn).mock.calls[0]?.[0]).toContain("@1735689600");

    // A different endpoint gets its own single warning.
    await client.listCapsules();
    await client.listCapsules();
    expect(console.warn).toHaveBeenCalledTimes(2);
    expect(vi.mocked(console.warn).mock.calls[1]?.[0]).toContain("GET /capsules");
  });

  it("does not warn and sets no meta fields when the headers are absent", async () => {
    const { fetchImpl } = recordingFetch([jsonResponse(capsuleBody)]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    const { meta } = await client.getCapsule("run-1");

    expect(meta.deprecation).toBeUndefined();
    expect(meta.sunset).toBeUndefined();
    expect(console.warn).not.toHaveBeenCalled();
  });
});
