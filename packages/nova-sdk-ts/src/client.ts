/**
 * NovaFabric TypeScript SDK — handwritten runtime client (ADR-0194).
 *
 * Thin typed wrapper over native `fetch` for the /v0 REST surface described
 * by api/openapi.yaml. Zero runtime dependencies. No default base URL, no
 * telemetry, no requests other than the ones the caller invokes.
 */
import type { components, operations } from "./types.gen.js";

// ---------------------------------------------------------------------------
// Re-exported contract types (generated from api/openapi.yaml)
// ---------------------------------------------------------------------------

export type ErrorDetail = components["schemas"]["ErrorDetail"];
export type ErrorEnvelope = components["schemas"]["ErrorEnvelope"];
export type PaginationMeta = components["schemas"]["PaginationMeta"];
export type AssetType = components["schemas"]["AssetType"];
export type AssetStatus = components["schemas"]["AssetStatus"];
export type AssetSummary = components["schemas"]["AssetSummary"];
export type AssetDetail = components["schemas"]["AssetDetail"];
export type AssetListResponse = components["schemas"]["AssetListResponse"];
export type CapsuleSummary = components["schemas"]["CapsuleSummary"];
export type CapsuleDetail = components["schemas"]["CapsuleDetail"];
export type CapsuleListResponse = components["schemas"]["CapsuleListResponse"];
/** Request body for `exportEvidence` — the capsule to seal into an Evidence Bundle. */
export type EvidenceExportRequest = components["schemas"]["EvidenceExportRequest"];
/** Evidence-bundle metadata returned by `exportEvidence` / `getEvidenceBundle`. */
export type BundleSummary = components["schemas"]["BundleSummary"];

/**
 * Request body for `submitScore` — an externally-computed evaluation record
 * appended to a run capsule's `scores.jsonl` (ADR-0119). Generated from the
 * `submitCapsuleScore` operation in api/openapi.yaml.
 */
export type ScoreSubmission =
  operations["submitCapsuleScore"]["requestBody"]["content"]["application/json"];

/**
 * `201 Created` body returned when a score is appended — the stored record plus
 * idempotency/config-binding flags. A `200` idempotent replay carries no body
 * (see `submitScore`, which returns `data: null` in that case).
 */
export type ScoreSubmissionResponse =
  operations["submitCapsuleScore"]["responses"][201]["content"]["application/json"];

// ---------------------------------------------------------------------------
// Client options and response metadata
// ---------------------------------------------------------------------------

/** Async token provider — the host application owns refresh (ADR-0194 D2). */
export type TokenProvider = () => Promise<string>;

export interface NovaFabricClientOptions {
  /**
   * Required — there is no default server URL (ADR-0194 D4; private
   * deployments are the norm). Include the versioned prefix, e.g.
   * `https://nova.example.com/v0` (api/openapi.yaml paths are relative to
   * the `/v0` server).
   */
  baseUrl: string;
  /**
   * Static bearer token (OIDC access token or ADR-0018 offline token), or a
   * callback returning one. Sent as `Authorization: Bearer <token>`. The SDK
   * performs no OIDC flows itself.
   */
  token?: string | TokenProvider;
  /** Injectable fetch implementation (tests, polyfills). Defaults to global fetch. */
  fetch?: typeof globalThis.fetch;
}

/** Per-response metadata surfaced alongside every decoded body. */
export interface ResponseMeta {
  /** HTTP status code. */
  status: number;
  /** RFC 9745 `Deprecation` header value, verbatim, when the server sent one (ADR-0188). */
  deprecation?: string;
  /** RFC 8594 `Sunset` header value, verbatim, when the server sent one (ADR-0188). */
  sunset?: string;
}

/** Decoded body plus response metadata (deprecation/sunset surfacing). */
export interface ApiResult<T> {
  data: T;
  meta: ResponseMeta;
}

/**
 * Coordinates for pointing an existing OTel JS trace exporter at the
 * deployment's OTLP ingest (ADR-0177 / ADR-0098). The SDK does NOT implement an
 * OTLP encoder — you bring your own exporter (`@opentelemetry/exporter-trace-otlp-http`)
 * and feed it these values. See `otlpTraceEndpoint`.
 */
export interface OtlpEndpoint {
  /** Absolute URL of the OTLP/HTTP traces endpoint (`.../api/otlp/v1/traces`). */
  url: string;
  /**
   * Headers to attach — carries `Authorization: Bearer <token>` when the client
   * was constructed with a token (empty object otherwise). The exporter sets its
   * own `Content-Type` (OTLP/JSON or OTLP/protobuf); the SDK does not.
   */
  headers: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/**
 * Typed error thrown for every non-2xx response, mapped from the standard
 * error envelope (`{"error": {"code", "message", "details?"}}`). Falls back
 * to code `"unknown_error"` when the body is not a valid envelope.
 */
export class NovaFabricApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | undefined;
  readonly meta: ResponseMeta;

  constructor(args: {
    status: number;
    code: string;
    message: string;
    details?: Record<string, unknown> | undefined;
    meta: ResponseMeta;
  }) {
    super(args.message);
    this.name = "NovaFabricApiError";
    this.status = args.status;
    this.code = args.code;
    this.details = args.details;
    this.meta = args.meta;
  }
}

// ---------------------------------------------------------------------------
// Deprecation warn-once registry (per process, per endpoint)
// ---------------------------------------------------------------------------

const warnedEndpoints = new Set<string>();

/** Test helper: clear the process-wide deprecation warn-once registry. */
export function resetDeprecationWarnings(): void {
  warnedEndpoints.clear();
}

function surfaceDeprecation(endpoint: string, meta: ResponseMeta): void {
  if (meta.deprecation === undefined && meta.sunset === undefined) return;
  if (warnedEndpoints.has(endpoint)) return;
  warnedEndpoints.add(endpoint);
  const parts = [`NovaFabric SDK: endpoint ${endpoint} is deprecated`];
  if (meta.deprecation !== undefined) parts.push(`Deprecation: ${meta.deprecation}`);
  if (meta.sunset !== undefined) parts.push(`Sunset: ${meta.sunset}`);
  parts.push("See the API deprecation register (ADR-0188).");
  console.warn(parts.join(" — "));
}

// ---------------------------------------------------------------------------
// Parameter shapes
// ---------------------------------------------------------------------------

export interface ListCapsulesParams {
  /** Max items per page (server default 50, max 500). */
  limit?: number;
  /** Opaque cursor from a previous page's `next_cursor`. */
  cursor?: string;
}

export interface ListAssetsParams {
  limit?: number;
  cursor?: string;
  asset_type?: AssetType;
  status?: AssetStatus;
}

type QueryValue = string | number | undefined;

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

export class NovaFabricClient {
  private readonly baseUrl: string;
  private readonly token: string | TokenProvider | undefined;
  private readonly fetchImpl: typeof globalThis.fetch;

  constructor(options: NovaFabricClientOptions) {
    if (typeof options?.baseUrl !== "string" || options.baseUrl.length === 0) {
      throw new TypeError(
        "NovaFabricClient requires a baseUrl (there is no default server URL). " +
          'Example: new NovaFabricClient({ baseUrl: "https://nova.example.com/v0" })',
      );
    }
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.token = options.token;
    // Bind to preserve `this` for the global fetch in browsers.
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  // ------------------------------ capsules -------------------------------

  /** GET /capsules/{run_id} — capsule detail by run ID. */
  async getCapsule(runId: string): Promise<ApiResult<CapsuleDetail>> {
    return this.request<CapsuleDetail>(
      "GET",
      `/capsules/${encodeURIComponent(runId)}`,
      "GET /capsules/{run_id}",
    );
  }

  /** GET /capsules — one page of capsule summaries (cursor pagination). */
  async listCapsules(
    params: ListCapsulesParams = {},
  ): Promise<ApiResult<CapsuleListResponse>> {
    return this.request<CapsuleListResponse>("GET", "/capsules", "GET /capsules", {
      limit: params.limit,
      cursor: params.cursor,
    });
  }

  /**
   * Async iterator over ALL capsules, walking `next_cursor` pages lazily.
   * Each page is fetched only as iteration reaches it.
   */
  async *iterateCapsules(
    params: ListCapsulesParams = {},
  ): AsyncGenerator<CapsuleSummary, void, undefined> {
    let cursor = params.cursor;
    for (;;) {
      const pageParams: ListCapsulesParams = {};
      if (params.limit !== undefined) pageParams.limit = params.limit;
      if (cursor !== undefined) pageParams.cursor = cursor;
      const { data } = await this.listCapsules(pageParams);
      for (const item of data.items) yield item;
      if (data.next_cursor === null || data.next_cursor === undefined) return;
      cursor = data.next_cursor;
    }
  }

  // ------------------------------- assets --------------------------------

  /** GET /assets/{id} — asset detail by registry UUID. */
  async getAsset(id: string): Promise<ApiResult<AssetDetail>> {
    return this.request<AssetDetail>(
      "GET",
      `/assets/${encodeURIComponent(id)}`,
      "GET /assets/{id}",
    );
  }

  /** GET /assets — one page of asset summaries (cursor pagination). */
  async listAssets(
    params: ListAssetsParams = {},
  ): Promise<ApiResult<AssetListResponse>> {
    return this.request<AssetListResponse>("GET", "/assets", "GET /assets", {
      limit: params.limit,
      cursor: params.cursor,
      asset_type: params.asset_type,
      status: params.status,
    });
  }

  // ------------------------------- scores --------------------------------

  /**
   * POST /capsules/{run_id}/scores — submit an externally-computed score into
   * the capsule's append-only `scores.jsonl` (ADR-0119; requires the
   * `scores:write` capability). Append-only and fail-closed: a correction is a
   * new record whose `supersedes` names a prior `score_id`.
   *
   * Returns the stored record (`ApiResult<ScoreSubmissionResponse>`) on a
   * `201 Created`. On a `200` idempotent replay (an identical body for a
   * `score_id` already present) the server appends no second line and returns
   * no body — `data` is then `null`; inspect `meta.status` to distinguish.
   */
  async submitScore(
    runId: string,
    score: ScoreSubmission,
  ): Promise<ApiResult<ScoreSubmissionResponse | null>> {
    const { response, meta } = await this.send(
      "POST",
      `/capsules/${encodeURIComponent(runId)}/scores`,
      "POST /capsules/{run_id}/scores",
      { body: score },
    );
    // 200 = idempotent replay, no body; 201 = the appended score record.
    if (response.status === 200) return { data: null, meta };
    const data = (await response.json()) as ScoreSubmissionResponse;
    return { data, meta };
  }

  // ------------------------------ evidence -------------------------------

  /**
   * POST /evidence — build a signed Evidence Bundle ZIP from a capsule
   * (ADR-0004; requires the `writer` role). Returns `202 Accepted` with the
   * {@link BundleSummary}; poll {@link getEvidenceBundle} until the bundle is
   * ready, then {@link downloadEvidenceBundle} to fetch the ZIP.
   */
  async exportEvidence(
    request: EvidenceExportRequest,
  ): Promise<ApiResult<BundleSummary>> {
    const { response, meta } = await this.send("POST", "/evidence", "POST /evidence", {
      body: request,
    });
    const data = (await response.json()) as BundleSummary;
    return { data, meta };
  }

  /**
   * GET /evidence/{bundle_id} — evidence-bundle metadata by id (requires the
   * `auditor` role). Poll this after {@link exportEvidence} to learn when the
   * bundle is on disk (`bundle_path` set, `size_bytes` > 0).
   */
  async getEvidenceBundle(bundleId: string): Promise<ApiResult<BundleSummary>> {
    return this.request<BundleSummary>(
      "GET",
      `/evidence/${encodeURIComponent(bundleId)}`,
      "GET /evidence/{bundle_id}",
    );
  }

  /**
   * GET /evidence/{bundle_id}/download — fetch the Evidence Bundle ZIP as raw
   * bytes (requires the `auditor` role). The body is binary
   * (`application/zip`), so this returns a `Uint8Array` rather than JSON — write
   * it to a file, or re-wrap it in a `Blob` in the browser.
   */
  async downloadEvidenceBundle(
    bundleId: string,
  ): Promise<ApiResult<Uint8Array>> {
    const { response, meta } = await this.send(
      "GET",
      `/evidence/${encodeURIComponent(bundleId)}/download`,
      "GET /evidence/{bundle_id}/download",
    );
    const data = new Uint8Array(await response.arrayBuffer());
    return { data, meta };
  }

  // -------------------------------- OTLP ---------------------------------

  /**
   * Coordinates for pointing an existing OTel JS trace exporter at this
   * deployment's OTLP ingest (ADR-0177 / ADR-0098). Configuration help only —
   * the SDK does NOT encode or send OTLP itself.
   *
   * The ingest lives on the deployment's serve/server surface at
   * `/api/otlp/v1/traces` (NOT under the `/v0` API prefix), so the URL is
   * derived by stripping a trailing `/v0` segment from `baseUrl`. The returned
   * `headers` carry the bearer token when one was configured; the exporter sets
   * its own `Content-Type`.
   *
   * Example (bring your own exporter):
   * ```ts
   * const { url, headers } = await client.otlpTraceEndpoint();
   * const exporter = new OTLPTraceExporter({ url, headers });
   * ```
   */
  async otlpTraceEndpoint(): Promise<OtlpEndpoint> {
    const root = this.baseUrl.replace(/\/v0$/, "");
    const url = `${root}/api/otlp/v1/traces`;
    const headers: Record<string, string> = {};
    const auth = await this.authHeader();
    if (auth !== undefined) headers["Authorization"] = auth;
    return { url, headers };
  }

  // ------------------------------ internals ------------------------------

  private async authHeader(): Promise<string | undefined> {
    if (this.token === undefined) return undefined;
    const raw = typeof this.token === "function" ? await this.token() : this.token;
    return `Bearer ${raw}`;
  }

  /**
   * Shared request core: builds the URL, applies auth + optional JSON body,
   * surfaces deprecation headers, and throws a typed error on any non-2xx.
   * Returns the raw `Response` so callers decode the body as their contract
   * requires (a JSON page, or an empty idempotent-replay 200).
   */
  private async send(
    method: string,
    path: string,
    endpoint: string,
    opts: { query?: Record<string, QueryValue>; body?: unknown } = {},
  ): Promise<{ response: Response; meta: ResponseMeta }> {
    const search = new URLSearchParams();
    if (opts.query !== undefined) {
      for (const [key, value] of Object.entries(opts.query)) {
        if (value !== undefined) search.set(key, String(value));
      }
    }
    const qs = search.toString();
    const url = `${this.baseUrl}${path}${qs.length > 0 ? `?${qs}` : ""}`;

    const headers = new Headers({ Accept: "application/json" });
    const auth = await this.authHeader();
    if (auth !== undefined) headers.set("Authorization", auth);

    const init: RequestInit = { method, headers };
    if (opts.body !== undefined) {
      headers.set("Content-Type", "application/json");
      init.body = JSON.stringify(opts.body);
    }

    const response = await this.fetchImpl(url, init);

    const meta: ResponseMeta = { status: response.status };
    const deprecation = response.headers.get("deprecation");
    if (deprecation !== null) meta.deprecation = deprecation;
    const sunset = response.headers.get("sunset");
    if (sunset !== null) meta.sunset = sunset;
    surfaceDeprecation(endpoint, meta);

    if (!response.ok) {
      throw await this.toApiError(response, meta);
    }
    return { response, meta };
  }

  private async request<T>(
    method: string,
    path: string,
    endpoint: string,
    query?: Record<string, QueryValue>,
  ): Promise<ApiResult<T>> {
    const opts = query === undefined ? {} : { query };
    const { response, meta } = await this.send(method, path, endpoint, opts);
    const data = (await response.json()) as T;
    return { data, meta };
  }

  private async toApiError(
    response: Response,
    meta: ResponseMeta,
  ): Promise<NovaFabricApiError> {
    let envelope: ErrorEnvelope | undefined;
    try {
      const body: unknown = await response.json();
      if (
        typeof body === "object" &&
        body !== null &&
        "error" in body &&
        typeof (body as ErrorEnvelope).error === "object" &&
        (body as ErrorEnvelope).error !== null &&
        typeof (body as ErrorEnvelope).error.code === "string" &&
        typeof (body as ErrorEnvelope).error.message === "string"
      ) {
        envelope = body as ErrorEnvelope;
      }
    } catch {
      // Non-JSON error body — fall through to the generic error below.
    }
    if (envelope !== undefined) {
      return new NovaFabricApiError({
        status: response.status,
        code: envelope.error.code,
        message: envelope.error.message,
        details: envelope.error.details,
        meta,
      });
    }
    return new NovaFabricApiError({
      status: response.status,
      code: "unknown_error",
      message: `HTTP ${response.status} ${response.statusText || ""}`.trim(),
      meta,
    });
  }
}
