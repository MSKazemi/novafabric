import { describe, expect, it } from "vitest";

import { NovaFabricClient, type CapsuleSummary } from "../src/index.js";
import { jsonResponse, recordingFetch } from "./helpers.js";

const capsule = (runId: string): CapsuleSummary => ({
  run_id: runId,
  status: "success",
  created_at: "2026-07-17T00:00:00Z",
});

describe("cursor pagination", () => {
  it("iterateCapsules walks next_cursor across pages and yields all items in order", async () => {
    const { fetchImpl, requests } = recordingFetch([
      jsonResponse({
        items: [capsule("run-1"), capsule("run-2")],
        next_cursor: "cursor-page-2",
        total: 5,
      }),
      jsonResponse({
        items: [capsule("run-3"), capsule("run-4")],
        next_cursor: "cursor-page-3",
        total: 5,
      }),
      jsonResponse({
        items: [capsule("run-5")],
        next_cursor: null,
        total: 5,
      }),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    const seen: string[] = [];
    for await (const item of client.iterateCapsules({ limit: 2 })) {
      seen.push(item.run_id);
    }

    expect(seen).toEqual(["run-1", "run-2", "run-3", "run-4", "run-5"]);
    expect(requests).toHaveLength(3);

    const urls = requests.map((r) => new URL(r.url));
    expect(urls.every((u) => u.pathname === "/v0/capsules")).toBe(true);
    expect(urls[0]?.searchParams.get("limit")).toBe("2");
    expect(urls[0]?.searchParams.get("cursor")).toBeNull();
    expect(urls[1]?.searchParams.get("cursor")).toBe("cursor-page-2");
    expect(urls[2]?.searchParams.get("cursor")).toBe("cursor-page-3");
    expect(urls[2]?.searchParams.get("limit")).toBe("2");
  });

  it("stops after a single page when next_cursor is null", async () => {
    const { fetchImpl, requests } = recordingFetch([
      jsonResponse({ items: [capsule("only")], next_cursor: null, total: 1 }),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    const seen: string[] = [];
    for await (const item of client.iterateCapsules()) seen.push(item.run_id);

    expect(seen).toEqual(["only"]);
    expect(requests).toHaveLength(1);
  });

  it("listCapsules returns one typed page and sends bearer auth from a token provider", async () => {
    const { fetchImpl, requests } = recordingFetch([
      jsonResponse({ items: [capsule("run-a")], next_cursor: "n", total: 2 }),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0/", // trailing slash normalized
      token: async () => "provider-token",
      fetch: fetchImpl,
    });

    const { data, meta } = await client.listCapsules({ limit: 1, cursor: "c0" });

    expect(data.items.map((i) => i.run_id)).toEqual(["run-a"]);
    expect(data.next_cursor).toBe("n");
    expect(data.total).toBe(2);
    expect(meta.status).toBe(200);
    const url = new URL(requests[0]!.url);
    expect(url.pathname).toBe("/v0/capsules");
    expect(url.searchParams.get("cursor")).toBe("c0");
    expect(requests[0]!.headers.get("authorization")).toBe("Bearer provider-token");
  });

  it("listAssets forwards filters and getAsset/getCapsule hit the documented paths", async () => {
    const { fetchImpl, requests } = recordingFetch([
      jsonResponse({ items: [], next_cursor: null, total: 0 }),
      jsonResponse({
        id: "8b1e...",
        name: "my-agent",
        version: "1.0.0",
        asset_type: "agent",
        status: "production",
        created_at: "2026-07-17T00:00:00Z",
      }),
      jsonResponse(capsule("run-z")),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      token: "static-token",
      fetch: fetchImpl,
    });

    await client.listAssets({ asset_type: "agent", status: "production" });
    await client.getAsset("asset id/with?chars");
    await client.getCapsule("run-z");

    const urls = requests.map((r) => new URL(r.url));
    expect(urls[0]?.pathname).toBe("/v0/assets");
    expect(urls[0]?.searchParams.get("asset_type")).toBe("agent");
    expect(urls[0]?.searchParams.get("status")).toBe("production");
    expect(urls[1]?.pathname).toBe("/v0/assets/asset%20id%2Fwith%3Fchars");
    expect(urls[2]?.pathname).toBe("/v0/capsules/run-z");
    expect(requests[0]!.headers.get("authorization")).toBe("Bearer static-token");
  });
});
