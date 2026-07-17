/**
 * NovaFabric TypeScript SDK — handwritten runtime client (ADR-0194).
 *
 * Thin typed wrapper over native `fetch` for the /v0 REST surface described
 * by api/openapi.yaml. Zero runtime dependencies. No default base URL, no
 * telemetry, no requests other than the ones the caller invokes.
 */
import type { components } from "./types.gen.js";

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

  // ------------------------------ internals ------------------------------

  private async authHeader(): Promise<string | undefined> {
    if (this.token === undefined) return undefined;
    const raw = typeof this.token === "function" ? await this.token() : this.token;
    return `Bearer ${raw}`;
  }

  private async request<T>(
    method: string,
    path: string,
    endpoint: string,
    query?: Record<string, QueryValue>,
  ): Promise<ApiResult<T>> {
    const search = new URLSearchParams();
    if (query !== undefined) {
      for (const [key, value] of Object.entries(query)) {
        if (value !== undefined) search.set(key, String(value));
      }
    }
    const qs = search.toString();
    const url = `${this.baseUrl}${path}${qs.length > 0 ? `?${qs}` : ""}`;

    const headers = new Headers({ Accept: "application/json" });
    const auth = await this.authHeader();
    if (auth !== undefined) headers.set("Authorization", auth);

    const response = await this.fetchImpl(url, { method, headers });

    const meta: ResponseMeta = { status: response.status };
    const deprecation = response.headers.get("deprecation");
    if (deprecation !== null) meta.deprecation = deprecation;
    const sunset = response.headers.get("sunset");
    if (sunset !== null) meta.sunset = sunset;
    surfaceDeprecation(endpoint, meta);

    if (!response.ok) {
      throw await this.toApiError(response, meta);
    }
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
