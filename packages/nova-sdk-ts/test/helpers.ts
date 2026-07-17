/** Shared test helpers — mock fetch, no network anywhere in this suite. */

export function jsonResponse(
  body: unknown,
  init: { status?: number; statusText?: string; headers?: Record<string, string> } = {},
): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    statusText: init.statusText ?? "",
    headers: { "content-type": "application/json", ...init.headers },
  });
}

export interface RecordedRequest {
  url: string;
  method: string;
  headers: Headers;
}

/**
 * A recording fetch double: captures every request and replays the given
 * responses in order (or via a responder function). Throws if the client
 * makes more requests than responses were provided — which also catches any
 * unsolicited request.
 */
export function recordingFetch(
  responses: Response[] | ((req: RecordedRequest) => Response),
): { fetchImpl: typeof globalThis.fetch; requests: RecordedRequest[] } {
  const requests: RecordedRequest[] = [];
  const queue = Array.isArray(responses) ? [...responses] : undefined;
  const fetchImpl = (async (
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const recorded: RecordedRequest = {
      url,
      method: init?.method ?? "GET",
      headers: new Headers(init?.headers),
    };
    requests.push(recorded);
    if (queue === undefined) {
      return (responses as (req: RecordedRequest) => Response)(recorded);
    }
    const next = queue.shift();
    if (next === undefined) {
      throw new Error(`unexpected request (no mock response left): ${url}`);
    }
    return next;
  }) as typeof globalThis.fetch;
  return { fetchImpl, requests };
}
