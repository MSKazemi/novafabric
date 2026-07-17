import { describe, expect, it } from "vitest";

import { NovaFabricApiError, NovaFabricClient } from "../src/index.js";
import { jsonResponse, recordingFetch } from "./helpers.js";

describe("error envelope mapping", () => {
  it("maps the standard error envelope to a typed NovaFabricApiError", async () => {
    const { fetchImpl } = recordingFetch([
      jsonResponse(
        {
          error: {
            code: "not_found",
            message: "Capsule 'run-missing' not found.",
            details: { run_id: "run-missing" },
          },
        },
        { status: 404 },
      ),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    const err = await client.getCapsule("run-missing").catch((e: unknown) => e);

    expect(err).toBeInstanceOf(NovaFabricApiError);
    const apiErr = err as NovaFabricApiError;
    expect(apiErr.status).toBe(404);
    expect(apiErr.code).toBe("not_found");
    expect(apiErr.message).toBe("Capsule 'run-missing' not found.");
    expect(apiErr.details).toEqual({ run_id: "run-missing" });
    expect(apiErr.meta.status).toBe(404);
    expect(apiErr.name).toBe("NovaFabricApiError");
  });

  it("falls back to unknown_error for a non-envelope JSON body", async () => {
    const { fetchImpl } = recordingFetch([
      jsonResponse({ detail: "totally different shape" }, { status: 500, statusText: "Internal Server Error" }),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    const err = (await client.listAssets().catch((e: unknown) => e)) as NovaFabricApiError;

    expect(err).toBeInstanceOf(NovaFabricApiError);
    expect(err.status).toBe(500);
    expect(err.code).toBe("unknown_error");
    expect(err.message).toContain("500");
  });

  it("falls back to unknown_error for a non-JSON error body", async () => {
    const { fetchImpl } = recordingFetch([
      new Response("<html>502 Bad Gateway</html>", {
        status: 502,
        statusText: "Bad Gateway",
        headers: { "content-type": "text/html" },
      }),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    const err = (await client.getAsset("x").catch((e: unknown) => e)) as NovaFabricApiError;

    expect(err).toBeInstanceOf(NovaFabricApiError);
    expect(err.status).toBe(502);
    expect(err.code).toBe("unknown_error");
  });

  it("constructor without a baseUrl throws at runtime too (no default server URL)", () => {
    // The compile-time guarantee lives in test/types.test-d.ts; this covers
    // plain-JS consumers.
    expect(
      () => new NovaFabricClient({} as unknown as { baseUrl: string }),
    ).toThrowError(TypeError);
    expect(
      () => new NovaFabricClient({ baseUrl: "" }),
    ).toThrowError(/requires a baseUrl/);
  });
});
