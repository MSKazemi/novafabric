/** Review go-collector-ts-packages-08 — every TV-5 request must carry the serve token. */
import { describe, expect, it } from "vitest";

import { tv5Url, tv5WsUrl } from "../../src/components/topology/TV5Panel.js";

describe("TV-5 URLs", () => {
  it("appends the encoded token to REST paths", () => {
    expect(tv5Url("/api/tv5/windows", "a b&c")).toBe("/api/tv5/windows?token=a%20b%26c");
    expect(tv5Url("/api/tv5/snapshot/w1", "t")).toBe("/api/tv5/snapshot/w1?token=t");
  });

  it("joins with & when the path already has a query", () => {
    expect(tv5Url("/x?a=1", "t")).toBe("/x?a=1&token=t");
  });

  it("builds the WebSocket URL with the token and the page's scheme", () => {
    expect(tv5WsUrl("t", { protocol: "http:", host: "127.0.0.1:4321" })).toBe(
      "ws://127.0.0.1:4321/api/tv5/ws?token=t",
    );
    expect(tv5WsUrl("t", { protocol: "https:", host: "h" })).toBe("wss://h/api/tv5/ws?token=t");
  });
});
