import { describe, expect, it } from "vitest";

import {
  NovaFabricApiError,
  NovaFabricClient,
  type ScoreSubmission,
} from "../src/index.js";
import { jsonResponse, recordingFetch } from "./helpers.js";

const DIGEST = `sha256:${"a".repeat(64)}`;

function validScore(): ScoreSubmission {
  return {
    name: "faithfulness",
    value: 0.92,
    value_type: "numeric",
    source: "judge",
    evaluator_id: "eval-01H",
    subject: DIGEST,
    subject_kind: "capsule",
    eval_card_digest: DIGEST,
  };
}

describe("submitScore (POST /capsules/{run_id}/scores)", () => {
  it("POSTs the score body and returns the appended record on 201", async () => {
    const { fetchImpl, requests } = recordingFetch([
      jsonResponse(
        {
          score: { score_id: "score-abc" },
          idempotent_replay: false,
          config_bound: true,
          submission: { principal: "svc:evals", scope: "scores:write" },
        },
        { status: 201 },
      ),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      token: "tok-123",
      fetch: fetchImpl,
    });

    const { data, meta } = await client.submitScore("run-01H", validScore());

    // One request, correct verb + URL (run_id path-encoded).
    expect(requests).toHaveLength(1);
    expect(requests[0]!.method).toBe("POST");
    expect(requests[0]!.url).toBe(
      "https://nova.example.com/v0/capsules/run-01H/scores",
    );
    expect(requests[0]!.headers.get("authorization")).toBe("Bearer tok-123");
    expect(requests[0]!.headers.get("content-type")).toBe("application/json");

    expect(meta.status).toBe(201);
    expect(data).not.toBeNull();
    expect(data!.idempotent_replay).toBe(false);
    expect(data!.config_bound).toBe(true);
  });

  it("path-encodes a run_id with reserved characters", async () => {
    const { fetchImpl, requests } = recordingFetch([
      jsonResponse(
        { score: {}, idempotent_replay: false, config_bound: true },
        { status: 201 },
      ),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    await client.submitScore("run/with space", validScore());

    expect(requests[0]!.url).toBe(
      "https://nova.example.com/v0/capsules/run%2Fwith%20space/scores",
    );
  });

  it("returns data:null on a 200 idempotent replay (no second line appended)", async () => {
    const { fetchImpl } = recordingFetch([
      new Response(null, { status: 200 }),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    const { data, meta } = await client.submitScore("run-01H", validScore());

    expect(meta.status).toBe(200);
    expect(data).toBeNull();
  });

  it("maps the error envelope to NovaFabricApiError on a 409 conflict", async () => {
    const { fetchImpl } = recordingFetch([
      jsonResponse(
        {
          error: {
            code: "score_conflict",
            message: "score_id already present with a different body.",
            details: { score_id: "score-abc" },
          },
        },
        { status: 409 },
      ),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      token: "tok",
      fetch: fetchImpl,
    });

    const err = (await client
      .submitScore("run-01H", validScore())
      .catch((e: unknown) => e)) as NovaFabricApiError;

    expect(err).toBeInstanceOf(NovaFabricApiError);
    expect(err.status).toBe(409);
    expect(err.code).toBe("score_conflict");
    expect(err.details).toEqual({ score_id: "score-abc" });
  });

  it("surfaces deprecation headers on the score endpoint too", async () => {
    const { fetchImpl } = recordingFetch([
      jsonResponse(
        { score: {}, idempotent_replay: false, config_bound: true },
        { status: 201, headers: { deprecation: "true", sunset: "Wed, 01 Jan 2031 00:00:00 GMT" } },
      ),
    ]);
    const client = new NovaFabricClient({
      baseUrl: "https://nova.example.com/v0",
      fetch: fetchImpl,
    });

    const { meta } = await client.submitScore("run-01H", validScore());

    expect(meta.deprecation).toBe("true");
    expect(meta.sunset).toBe("Wed, 01 Jan 2031 00:00:00 GMT");
  });
});
