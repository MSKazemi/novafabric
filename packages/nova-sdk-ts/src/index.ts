/**
 * @novafabric/sdk — official TypeScript client for the NovaFabric /v0 REST
 * API (ADR-0194). Experimental.
 */
export {
  NovaFabricClient,
  NovaFabricApiError,
  resetDeprecationWarnings,
} from "./client.js";
export type {
  NovaFabricClientOptions,
  TokenProvider,
  ResponseMeta,
  ApiResult,
  OtlpEndpoint,
  ScoreSubmission,
  ScoreSubmissionResponse,
  ListCapsulesParams,
  ListAssetsParams,
  ErrorDetail,
  ErrorEnvelope,
  PaginationMeta,
  AssetType,
  AssetStatus,
  AssetSummary,
  AssetDetail,
  AssetListResponse,
  CapsuleSummary,
  CapsuleDetail,
  CapsuleListResponse,
} from "./client.js";
export type { paths, components, operations } from "./types.gen.js";
