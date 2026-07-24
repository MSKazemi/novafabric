import { describe, expect, it } from "vitest";

import {
  type BundleSummary,
  type EvidenceExportRequest,
  NovaFabricApiError,
  NovaFabricClient,
} from "../src/index.js";
import { jsonResponse, recordingFetch } from "./helpers.js";

function bundle(): BundleSummary {
  return {
    bundle_id: "bnd-123",
    run_id: "run-01H",
    created_at: "2026-07-24T00:00:00Z",
    size_bytes: 4096,
    bundle_path: "/srv/.novafabric/evidence/run-01H.zip",
  };
}

describe("exportEvidence (POST /evidence)", () => {
  it("POSTs the request body and returns the bundle summary on 202", async () => {
    const { fetchImpl, requests } = recordingFetch([
      jsonResponse(bundle(), { status: 202 }),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      token: "tok-123",
      fetch: fetchImpl,
    });

    const req: EvidenceExportRequest = { run_id: "run-01H", allow_unsafe_skips: false };
    const { data, meta } = await client.exportEvidence(req);

    expect(requests).toHaveLength(1);
    expect(requests[0]!.method).toBe("POST");
    expect(requests[0]!.url).toBe("https://nova.example.com/v0/evidence");
    expect(requests[0]!.headers.get("authorization")).toBe("Bearer tok-123");
    expect(requests[0]!.headers.get("content-type")).toBe("application/json");
    expect(meta.status).toBe(202);
    expect(data.bundle_id).toBe("bnd-123");
    expect(data.run_id).toBe("run-01H");
  });

  it("throws a typed error when the capsule is not found (404)", async () => {
    const { fetchImpl } = recordingFetch([
      jsonResponse(
        { error: { code: "not_found", message: "Capsule 'x' not found." } },
        { status: 404 },
      ),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    await expect(
      client.exportEvidence({ run_id: "x", allow_unsafe_skips: false }),
    ).rejects.toBeInstanceOf(NovaFabricApiError);
  });
});

describe("getEvidenceBundle (GET /evidence/{bundle_id})", () => {
  it("path-encodes the bundle id and returns the summary", async () => {
    const { fetchImpl, requests } = recordingFetch([jsonResponse(bundle())]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    const { data } = await client.getEvidenceBundle("bnd/123");

    expect(requests[0]!.method).toBe("GET");
    expect(requests[0]!.url).toBe("https://nova.example.com/v0/evidence/bnd%2F123");
    expect(data.size_bytes).toBe(4096);
  });
});

describe("downloadEvidenceBundle (GET /evidence/{bundle_id}/download)", () => {
  it("returns the ZIP bytes as a Uint8Array", async () => {
    const zipBytes = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x00, 0x01]); // "PK\x03\x04"
    const { fetchImpl, requests } = recordingFetch([
      new Response(zipBytes, {
        status: 200,
        headers: { "content-type": "application/zip" },
      }),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      token: "tok-123",
      fetch: fetchImpl,
    });

    const { data, meta } = await client.downloadEvidenceBundle("bnd-123");

    expect(requests[0]!.method).toBe("GET");
    expect(requests[0]!.url).toBe(
      "https://nova.example.com/v0/evidence/bnd-123/download",
    );
    expect(meta.status).toBe(200);
    expect(data).toBeInstanceOf(Uint8Array);
    expect(Array.from(data.slice(0, 4))).toEqual([0x50, 0x4b, 0x03, 0x04]);
  });

  it("throws a typed error when the bundle is missing (404)", async () => {
    const { fetchImpl } = recordingFetch([
      jsonResponse(
        { error: { code: "not_found", message: "not found" } },
        { status: 404 },
      ),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    await expect(
      client.downloadEvidenceBundle("missing"),
    ).rejects.toBeInstanceOf(NovaFabricApiError);
  });
});
