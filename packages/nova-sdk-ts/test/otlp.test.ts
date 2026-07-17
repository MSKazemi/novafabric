import { describe, expect, it } from "vitest";

import { NovaFabricClient } from "../src/index.js";
import { recordingFetch } from "./helpers.js";

describe("otlpTraceEndpoint (ADR-0177 ingest config helper)", () => {
  it("derives the server-root ingest URL by stripping the /v0 prefix", async () => {
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
    });

    const { url, headers } = await client.otlpTraceEndpoint();

    // /api/otlp/v1/traces lives on the serve surface, NOT under /v0.
    expect(url).toBe("https://nova.example.com/api/otlp/v1/traces");
    expect(headers).toEqual({});
  });

  it("attaches Authorization: Bearer from a static token", async () => {
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      token: "tok-static",
    });

    const { url, headers } = await client.otlpTraceEndpoint();

    expect(url).toBe("https://nova.example.com/api/otlp/v1/traces");
    expect(headers).toEqual({ Authorization: "Bearer tok-static" });
  });

  it("resolves an async token-provider callback for the header", async () => {
    let calls = 0;
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      token: async () => {
        calls += 1;
        return "tok-fresh";
      },
    });

    const { headers } = await client.otlpTraceEndpoint();

    expect(calls).toBe(1);
    expect(headers).toEqual({ Authorization: "Bearer tok-fresh" });
  });

  it("handles a base URL with a path prefix and trailing slash", async () => {
    const client = new NovaFabricClient({
      // constructor strips the trailing slash; /v0 segment is then stripped here.
      baseUrl: "https://nova.example.com/api/v0/",
    });

    const { url } = await client.otlpTraceEndpoint();

    expect(url).toBe("https://nova.example.com/api/api/otlp/v1/traces");
  });

  it("makes no network request — it only returns configuration", async () => {
    const { fetchImpl, requests } = recordingFetch([]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      token: "tok",
      fetch: fetchImpl,
    });

    await client.otlpTraceEndpoint();

    expect(requests).toHaveLength(0);
  });
});
