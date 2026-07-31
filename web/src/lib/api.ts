/**
 * Typed fetch client for `nova serve --experimental`.
 *
 * The dashboard runs in the browser; it reads a session token from
 * localStorage (set on the connect page), passes it as ?token=… on every
 * request, and treats the local server as untrusted enough to validate
 * Host headers (handled server-side).
 *
 * All endpoints are read-only in v0.7 (Layer A per ADR-0027). Mutating
 * endpoints land in v0.8 / v0.9.
 */

const TOKEN_KEY = 'novafabric.serve-token';
const BASE_KEY = 'novafabric.serve-base';

export interface AnalyticsBucket {
  bucket: string;
  run_count: number;
  failed_count: number;
  model_call_count: number;
  tool_call_count: number;
  duration_ms_p50: number | null;
  duration_ms_p95: number | null;
  duration_ms_max: number | null;
}

export interface AnalyticsSummary {
  buckets: AnalyticsBucket[];
  totals: {
    run_count: number;
    failed_count: number;
    model_call_count: number;
    tool_call_count: number;
  };
  since: string | null;
  until: string | null;
}

// P4 dashboard query panel — POST /api/query (ADR-0129 read surface).
export interface QueryPanelResult {
  schema_version: string;
  generated_at: string;
  query: Record<string, unknown>;
  time_window: { since: string; until: string };
  columns: string[];
  rows: Array<Record<string, unknown>>;
  row_count: number;
  truncated: boolean;
  index: { engine: string; built_at: string; capsule_count: number };
  cli_equivalent: string;
}

// ADR-0192 operational alerts (read model for the dashboard).
export type AlertOutcome =
  | 'emitted'
  | 'delivered'
  | 'failed'
  | 'dropped'
  | 'deduped'
  | 'no-endpoint';
export type AlertSeverity = 'info' | 'warning' | 'critical';

export interface AlertRow {
  id: string;
  timestamp: string;
  event_type: string;
  severity: AlertSeverity | null;
  subject: string;
  outcome: AlertOutcome;
  endpoint_id: string | null;
  attempts: number;
}

export interface AlertsRecentResult {
  alerts: AlertRow[];
  total: number;
  alerting_configured: boolean;
}

// ADR-0193 API keys (read-only view for the admin console; never secrets/hashes).
export type ApiKeyStatus = 'active' | 'revoked' | 'expired';

export interface ApiKeyRow {
  key_id: string;
  owner: string;
  roles: string[];
  workspace: string | null;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  status: ApiKeyStatus;
}

export interface AdminApiKeysResult {
  keys: ApiKeyRow[];
  total: number;
}

export interface RunSummary {
  run_id: string;
  status: string | null;
  created_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  exit_code: number | null;
  model_call_count: number;
  tool_call_count: number;
  mutating_tool_count: number;
  command: string[];
  novafabric_version: string | null;
  capsule_path: string;
}

export interface CapsuleFileMeta {
  name: string;
  size_bytes: number;
}

export interface FullCapsule {
  run_id: string;
  capsule_path: string;
  /** False when the capsule directory is missing on disk and only indexed
   *  metadata (manifest) is available — sub-file lists will be empty. */
  capsule_available?: boolean;
  manifest: Record<string, unknown>;
  trace: Array<Record<string, unknown>>;
  model_calls: Array<Record<string, unknown>>;
  tool_calls: Array<Record<string, unknown>>;
  lineage: Array<Record<string, unknown>>;
  assets: Array<Record<string, unknown>>;
  inputs: CapsuleFileMeta[];
  outputs: CapsuleFileMeta[];
}

export interface AssetSummary {
  id: string;
  name: string;
  version: string;
  asset_type: string;
  status: string;
  created_at: string;
  promoted_at: string | null;
  git_commit_sha: string | null;
}

export interface AssetDetail extends AssetSummary {
  spec_json?: string;
  eval_results: Array<{ suite_name: string; passed: boolean; score: string; run_at: string }>;
}

export interface LineageRecord {
  node_id: string;
  kind: string;
  ref: string;
  payload: string;
}

export interface CapsuleTreeNode {
  run_id: string;
  status: string;
  capsule_role: string;
  is_synthetic: boolean;
  children: CapsuleTreeNode[];
}

export interface RedactionFinding {
  finding_id: string;
  rule_id: string;
  rule_version: string;
  pack: string;
  severity: 'critical' | 'high' | 'medium' | 'low' | 'info';
  target_kind: string;
  target_ref: string;
  byte_offset: number;
  byte_length: number;
  match_hash: string;
  redaction_strategy: string;
  replacement: string;
}

export interface RedactionTarget {
  kind: string;
  ref: string;
  bytes_scanned: number;
  findings_count: number;
  skipped: boolean;
  binary: boolean;
  hash_before_redaction: string;
  hash_after_redaction: string;
}

export interface RedactionProof {
  schema_version: string;
  proof_id: string;
  capsule_run_id: string;
  created_at: string;
  scanner: { name: string; version: string; engine: string; engine_version: string };
  packs: Array<{ name: string; version: string; rules_count: number; rules_hash: string }>;
  targets: RedactionTarget[];
  findings_count: { total: number; by_severity: Record<string, number> };
  findings: RedactionFinding[];
  bytes_scanned: number;
  bytes_redacted: number;
  chain_hash: string;
}

export class ServeApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ServeApiError';
  }
}

function getCfg(): { token: string | null; base: string } {
  if (typeof localStorage === 'undefined') return { token: null, base: 'http://127.0.0.1:4321' };
  return {
    token: localStorage.getItem(TOKEN_KEY),
    base: localStorage.getItem(BASE_KEY) ?? 'http://127.0.0.1:4321',
  };
}

export function setConnection(token: string, base: string): void {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(BASE_KEY, base.replace(/\/$/, ''));
}

export function clearConnection(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(BASE_KEY);
}

export function getConnection(): { token: string | null; base: string } {
  return getCfg();
}

type Listener = () => void;
const listeners: Set<Listener> = new Set();

/** Subscribe to auth-state changes (e.g. 401 → cleared). DashboardApp uses this to bounce back to ConnectPanel. */
export function onAuthChange(fn: Listener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

function emitAuthChange(): void {
  for (const fn of listeners) {
    try { fn(); } catch { /* ignore */ }
  }
}

async function request<T>(path: string, query: Record<string, unknown> = {}): Promise<T> {
  const { token, base } = getCfg();
  if (!token) throw new ServeApiError(401, 'no token configured — connect first');
  const params = new URLSearchParams({ token, ...Object.fromEntries(Object.entries(query).map(([k, v]) => [k, String(v)])) });
  const res = await fetch(`${base}${path}?${params.toString()}`);
  if (res.status === 401) {
    // Stale or invalid token — wipe localStorage and notify the app to drop back to the connect screen.
    clearConnection();
    emitAuthChange();
    throw new ServeApiError(401, 'session expired — reconnect with the current token from the terminal');
  }
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body === 'object' && 'detail' in body) message = String(body.detail);
    } catch {
      /* ignore */
    }
    throw new ServeApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const { token, base } = getCfg();
  if (!token) throw new ServeApiError(401, 'no token configured — connect first');
  const params = new URLSearchParams({ token });
  const res = await fetch(`${base}${path}?${params.toString()}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (res.status === 401) {
    clearConnection();
    emitAuthChange();
    throw new ServeApiError(401, 'session expired — reconnect');
  }
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b && typeof b === 'object' && 'detail' in b) message = String(b.detail);
    } catch { /* ignore */ }
    throw new ServeApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

async function deleteRequest<T>(path: string, query: Record<string, string> = {}): Promise<T> {
  const { token, base } = getCfg();
  if (!token) throw new ServeApiError(401, 'no token configured — connect first');
  const params = new URLSearchParams({ token, ...query });
  const res = await fetch(`${base}${path}?${params.toString()}`, { method: 'DELETE' });
  if (res.status === 401) {
    clearConnection();
    emitAuthChange();
    throw new ServeApiError(401, 'session expired — reconnect');
  }
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const b = await res.json();
      if (b && typeof b === 'object' && 'detail' in b) message = String(b.detail);
    } catch { /* ignore */ }
    throw new ServeApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

export async function ping(base: string): Promise<{ ok: boolean; version: string }> {
  const res = await fetch(`${base.replace(/\/$/, '')}/api/health`);
  if (!res.ok) throw new ServeApiError(res.status, 'health check failed');
  return res.json();
}

/** Validate a token against an authenticated endpoint. Used at connect time to catch
 *  wrong/stale tokens before the user sees a 401 in some other tab. */
export async function validateToken(base: string, token: string): Promise<void> {
  const res = await fetch(`${base.replace(/\/$/, '')}/api/runs?token=${encodeURIComponent(token)}`);
  if (res.status === 401) throw new ServeApiError(401, 'token rejected');
  if (!res.ok) throw new ServeApiError(res.status, `validation failed: ${res.statusText}`);
}

export interface EvidenceSummary {
  bundle_id: string;
  run_id: string;
  timestamp: string;
  size_bytes: number;
  verified: boolean;
}

export interface EvidenceDetail {
  bundle_id: string;
  run_id: string;
  timestamp: string;
  size_bytes: number;
  manifest: Record<string, unknown>;
  dsse_statement: Record<string, unknown>;
  dsse_envelope: Record<string, unknown>;
  signing_key_fingerprint: string | null;
  files: Array<{ path: string; size_bytes: number }>;
}

export interface VerifyResult {
  bundle_id: string;
  run_id: string;
  valid: boolean;
  signature_ok: boolean;
  timestamp_ok: boolean | null;
  log_integrity_ok: boolean | null;
  seal_available: boolean;
  errors: string[];
}

export interface EvalHistoryResponse {
  asset_id: string;
  history: Array<{
    eval_id: string;
    suite_name: string;
    passed: boolean;
    score: number | null;
    run_at: string;
  }>;
}

export interface AssetDiffEntry {
  key: string;
  from?: unknown;
  to?: unknown;
  value?: unknown;
}

export interface AssetDiffResult {
  name: string;
  from_version: string;
  to_version: string;
  changed: Array<{ key: string; from: unknown; to: unknown }>;
  added: Array<{ key: string; value: unknown }>;
  removed: Array<{ key: string; value: unknown }>;
  identical: boolean;
}

export interface HoldRecord {
  hold_id: string;
  registry: string;
  reason: string;
  duration_days: number | null;
  created_at: string;
  released_at: string | null;
}

export interface HoldRegistry {
  name: string;
  holds: HoldRecord[];
}

export interface HoldsListResult {
  total_active: number;
  registries: HoldRegistry[];
}

export interface LineageEdgePayload {
  edge_id: string;
  edge_type: 'consumed' | 'produced_by' | 'replayed_from' | 'evaluated_by' | 'derived_from' | 'attested_by' | 'contains' | 'delegated_to' | 'spawned';
  source: string;
  target: string;
  capsule_run_id: string;
  confidence: string;
  created_at: string;
  direction: string;
  schema_version: string;
  emitter: { name: string; version: string };
}

// ---------- Policy check (DC-5) ----------

export interface PolicySubject {
  user: string;
  roles: string[];
}

export interface PolicyResource {
  kind: string;
  ref: string;
  eval_score?: number;
  unsafe_skips?: number;
  redaction_proof_present?: boolean;
}

export interface PolicyInput {
  action: string;
  subject: PolicySubject;
  resource: PolicyResource;
  context?: Record<string, unknown>;
}

export interface PolicyDecision {
  allow: boolean;
  reason: string;
  decision_id: string;
  policy_path: string;
  trace_text?: string;
}

export interface RegistrationSuggestion {
  asset_type: string;
  detected_name: string;
  detected_version: string;
  confidence: number;
  call_count: number;
  draft_spec_yaml: string;
  warnings: string[];
}

export interface SuggestionsResult {
  total_capsules_analyzed: number;
  suggestions: RegistrationSuggestion[];
}

// ---------- Tool permission events (cap-004) ----------

export interface ToolPermissionEventRecord {
  entity_type: 'tool_permission_event';
  capsule_id: string;
  event_id: string;
  tool_name: string;
  tool_id: string;
  policy_id: string;
  permission_level: 'read' | 'write' | 'execute' | 'network' | 'filesystem' | 'escalated';
  decision: 'allowed' | 'denied' | 'escalated_to_human';
  authorising_identity: string;
  human_approval_required: boolean;
  human_approval_obtained: boolean | null;
  approval_principal: string | null;
  decision_latency_ms: number;
  capsule_timestamp_utc: string;
}

// ---------- NovaSeal maker-checker (ADR-0059) ----------

export interface SealPolicyPredicate {
  proposer_key_ids?: string[];
  approver_key_ids?: string[];
  bypass_valid_duration_hours?: number;
  self_approval?: boolean;
  approval_threshold?: number;
  [key: string]: unknown;
}

export interface SealPolicyResponse {
  /** false when no promotion policy has been signed yet (200, not 404). */
  configured?: boolean;
  version: number | null;
  predicate: SealPolicyPredicate | null;
  created_at: string | null;
  detail?: string;
}

export interface SealProposalSummary {
  uuid: string;
  capsule_id: string;
  proposer_subject: string;
  justification: string;
  timestamp: string;
  policy_version: string;
  has_approval: boolean;
  approver_subject: string | null;
  approval_timestamp: string | null;
}

export interface SealCheckResult {
  check: number;
  name: string;
  passed: boolean;
  message: string;
}

export interface SealVerifyResponse {
  capsule_id: string;
  passed: boolean;
  exit_code: number;
  message: string;
  check_results: SealCheckResult[];
}

export interface SealBypassRequest {
  reason: string;
  duration_hours: number;
  key_pem: string;
  cert_pem: string;
  target_env: string;
}

export interface SealBypassResponse {
  bypass_uuid: string;
  capsule_id: string;
  authorized_by: string;
  valid_until: string;
  target_env: string;
}

// ---------- Admin: token + role management (DD-8) ----------

export interface TokenRecord {
  label: string;
  fingerprint: string;
  created_at: string;
  revoked: boolean;
}

export interface TokensListResult {
  tokens: TokenRecord[];
  session_token_fingerprint: string;
}

export interface IssueTokenResult {
  ok: boolean;
  token: string;
  fingerprint: string;
  label: string;
  warning: string;
}

export interface RevokeTokenResult {
  ok: boolean;
  fingerprint: string;
  revoked: boolean;
}

export interface RolesListResult {
  server_mode: boolean;
  roles: unknown[];
  message: string;
}

// ---------- B-1: cursor-based run search ----------

export interface RunSearchPage {
  items: RunSummary[];
  next_cursor: string | null;
  total_approx: number;
}

// ---------- B-4: stats with approximate flag ----------

export interface StatsResult {
  run_count: number;
  failed_run_count: number;
  passed_run_count: number;
  asset_count: number;
  pending_eval_count: number;
  production_asset_count: number;
  approximate?: boolean;
  cached_at?: string | null;
}

// ---------- Capsule verify result (v0.26.5) ----------

export interface CapsuleVerifyResult {
  sealed: boolean;
  configured: boolean | null;
  capsule_id?: string;
  signature_ok?: boolean;
  timestamp_ok?: boolean;
  log_integrity_ok?: boolean;
  valid?: boolean;
  errors?: string[];
  message?: string;
}

// ---------- OpenLineage export result (v0.26.5) ----------

export interface OpenLineageExportResult {
  ok: boolean;
  run_id: string;
  event_count: number;
  events: unknown[];
  error?: string;
}

// ---------- B-3: SSE stream helper ----------

/** Open an SSE stream to /api/runs/stream. Returns the EventSource. Close it to stop. */
export function openRunStream(onRun: (run: RunSummary) => void): EventSource {
  const { token, base } = getCfg();
  const url = `${base}/api/runs/stream?token=${encodeURIComponent(token ?? '')}`;
  const es = new EventSource(url);
  es.onmessage = (ev) => {
    try {
      const run = JSON.parse(ev.data) as RunSummary;
      onRun(run);
    } catch { /* ignore malformed event */ }
  };
  return es;
}

export interface RunStreamHandle {
  /** Stop the stream and cancel any pending reconnect. */
  close(): void;
}

/**
 * Managed SSE stream with capped exponential-backoff reconnect.
 *
 * The browser's EventSource auto-retries transient drops, but gives up
 * (readyState === CLOSED) on hard failures like token expiry or a long server
 * outage. This wrapper detects that terminal state and reconnects itself,
 * backing off 1s → 30s and resetting on a successful (re)connection. `onStatus`
 * reports live connection state so the UI can show "reconnecting…".
 */
/** Tunables for the exponential-backoff reconnect loop (exported for tests). */
export const RUN_STREAM_BACKOFF = {
  /** First reconnect delay, in ms. */
  baseMs: 1000,
  /** Hard cap on the reconnect delay, in ms. */
  maxMs: 30000,
  /** Multiplier applied per consecutive failure. */
  factor: 2,
} as const;

/**
 * Pure backoff calculator. Given the number of consecutive failed attempts
 * (1 = first retry), returns the delay before the next attempt, capped at
 * `maxMs`. Deterministic and side-effect free so it can be unit-tested without
 * a live EventSource.
 */
export function nextBackoffDelay(
  attempt: number,
  opts: { baseMs?: number; maxMs?: number; factor?: number } = {},
): number {
  const baseMs = opts.baseMs ?? RUN_STREAM_BACKOFF.baseMs;
  const maxMs = opts.maxMs ?? RUN_STREAM_BACKOFF.maxMs;
  const factor = opts.factor ?? RUN_STREAM_BACKOFF.factor;
  const n = Math.max(1, Math.floor(attempt));
  const delay = baseMs * Math.pow(factor, n - 1);
  return Math.min(delay, maxMs);
}

export function openManagedRunStream(
  onRun: (run: RunSummary) => void,
  onStatus?: (connected: boolean) => void,
): RunStreamHandle {
  const { token, base } = getCfg();
  const url = `${base}/api/runs/stream?token=${encodeURIComponent(token ?? '')}`;
  let es: EventSource | null = null;
  let closed = false;
  let attempt = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const connect = (): void => {
    if (closed) return;
    es = new EventSource(url);
    es.onopen = () => { attempt = 0; onStatus?.(true); };
    es.onmessage = (ev) => {
      try { onRun(JSON.parse(ev.data) as RunSummary); } catch { /* ignore malformed event */ }
    };
    es.onerror = () => {
      onStatus?.(false);
      // CONNECTING => the browser is already retrying; only step in once it gives up.
      if (es && es.readyState === EventSource.CLOSED) {
        es.close();
        es = null;
        if (!closed) {
          attempt += 1;
          timer = setTimeout(connect, nextBackoffDelay(attempt));
        }
      }
    };
  };

  connect();

  return {
    close(): void {
      closed = true;
      if (timer) { clearTimeout(timer); timer = null; }
      if (es) { es.close(); es = null; }
    },
  };
}

export interface IncidentDeadline {
  obligation: string;
  basis: string;
  anchor: string;
  deadline: string;
  days_remaining: number;
  overdue: boolean;
}

export interface IncidentView {
  id: string;
  title: string;
  classification: string;
  severity: string;
  status: string;
  occurred_at: string;
  aware_at: string | null;
  run_ids: string[];
  deadlines: IncidentDeadline[];
  nearest_deadline?: string;
  nearest_obligation?: string;
  hours_remaining?: number;
  overdue?: boolean;
}

// ---------- Accountability Spine response shapes (ADR-0093/0094/0095) ----------
export type EnergyConfidence = 'measured' | 'apportioned' | 'declared' | 'unknown';

export interface EnergyReceipt {
  action_ref: { kind: string; id: string };
  measured_joules: number | null;
  measurement_source: string;
  confidence: EnergyConfidence;
  carbon_g_co2e: number | null;
}

export interface EnergyConservation {
  status: string;
  attributed_joules_sum: number;
  node_measured_joules: number | null;
  divergence_pct: number | null;
  receipts_total: number;
  receipts_measured: number;
  receipts_declared: number;
  receipts_unavailable: number;
}

export interface EnergyResponse {
  receipts: EnergyReceipt[];
  conservation: EnergyConservation | null;
}

export interface LedgerResponse {
  ok: boolean;
  status: string;
  exit_code: number;
  reasons: string[];
  stream_verdicts: Record<string, string>;
}

// P5 — forensics timeline (GET /api/runs/{run_id}/forensics-timeline).
// Deterministic reconstruction from the run's own sealed capsule via the same
// forensics.timeline.merge_timeline core `nova forensics timeline` uses.
export interface ForensicsTimelineEvent {
  ts: string; // sealed ISO-8601 timestamp (verbatim, never the system clock)
  source_capsule: string;
  seq: number;
  kind: string; // "run" | "model-call" | "tool-call" | ...
  detail: string | null;
}
export interface ForensicsTimeline {
  incident_id: string;
  events: ForensicsTimelineEvent[];
  gaps: string[]; // evidence that could not be reconstructed (deduped, sorted)
  honesty_line: string;
}

// Trust surfaces (ADR-0173 radar / ADR-0174 redaction x-ray), read-only.
// A guarantee the capsule cannot evidence is reported "na", never "fail" —
// an unsealed capsule is unverified, not failed.
export type RadarAxisState = 'ok' | 'warn' | 'fail' | 'na';
export type RadarVerdict = 'attested' | 'partial' | 'critical' | 'unsealed';
export interface RadarAxis {
  key: string;
  label: string;
  value: number | null; // 0..1, null when not applicable
  state: RadarAxisState;
}
export interface TrustRadar {
  capsule_id: string | null;
  axes: RadarAxis[];
  verdict: RadarVerdict;
}

// ADR-0174 §1 invariant: paths and states only — a field VALUE is never returned.
export type FieldState =
  | 'clear'
  | 'redacted'
  | 'secret_scrubbed'
  | 'never_captured'
  | 'unknown';
export interface FieldXRay {
  path: string;
  state: FieldState;
}
export interface XRayReport {
  capsule_id: string | null;
  fields: FieldXRay[];
  counts: Record<string, number>;
  sensitive_total: number;
  sensitive_protected: number;
  coverage: number | null;
}

// P6 — cost-analytics trio (POST compute endpoints wrapping the nova cost cores).
export interface SpendAttribution {
  total_spend: number;
  productive_spend: number;
  wasted_spend: number;
  wasted_fraction: number;
  productive_statuses: string[];
  by_status: Record<string, number>;
}
export interface FairnessMetric {
  dimension: string;
  shares: Record<string, number>;
  gini: number;
  max_mean_ratio: number;
}
export interface FairnessReport {
  metrics: FairnessMetric[];
}
export interface UsageBreakdown {
  counted_tokens: number;
  composition: Record<string, number>;
  cached_read_ratio: number | null;
  has_reasoning_tokens: boolean;
  is_multimodal: boolean;
  extra_total: number;
}

export type SafetyBackingState = 'SUPPORTED' | 'UNSUPPORTED' | 'CONTESTED' | 'UNKNOWN';

export interface SafetyCaseNode {
  node_type: string;
  id: string;
  statement?: string;
  backing_state?: SafetyBackingState;
  contest_reason?: string | null;
  [key: string]: unknown;
}

export interface SafetyCaseResponse {
  template_id: string;
  top_claim_id: string;
  nodes: SafetyCaseNode[];
  residual_risk: unknown;
  case_hash: string;
}

export const api = {
  listRuns: (q: { limit?: number; offset?: number; since?: string; until?: string; status?: string; q?: string } = {}) =>
    request<{ count: number; total: number; has_more: boolean; limit: number; offset: number; capsule_dir: string; runs: RunSummary[] }>('/api/runs', q as Record<string, unknown>),

  // B-1: cursor-based search
  searchRuns: (q: { cursor?: string | null; limit?: number; q?: string; status?: string; since?: string; until?: string } = {}) => {
    const params: Record<string, unknown> = { limit: q.limit ?? 50 };
    if (q.cursor) params.cursor = q.cursor;
    if (q.q) params.q = q.q;
    if (q.status) params.status = q.status;
    if (q.since) params.since = q.since;
    if (q.until) params.until = q.until;
    return request<RunSearchPage>('/api/runs/search', params);
  },

  getRun: (run_id: string) => request<FullCapsule>(`/api/runs/${encodeURIComponent(run_id)}`),

  // Analytics summary — pre-aggregated day buckets from the runs index.
  analyticsSummary: (q: { since?: string; until?: string } = {}) => {
    const params: Record<string, unknown> = {};
    if (q.since) params.since = q.since;
    if (q.until) params.until = q.until;
    return request<AnalyticsSummary>('/api/analytics/summary', params);
  },
  // P4 — custom query panel: POST /api/query, {q, engine?} where q is a
  // Capsule Query DSL JSON/YAML document (same shape as `nova query --query-file`).
  runQuery: (q: string, engine?: string) =>
    postJson<QueryPanelResult>('/api/query', engine ? { q, engine } : { q }),

  getRunFile: (run_id: string, filepath: string) =>
    request<{ filename: string; content: string }>(`/api/runs/${encodeURIComponent(run_id)}/file/${filepath}`),

  // ---------- Accountability Spine (ADR-0093/0094/0095, read-only) ----------
  getRunEnergy: (run_id: string) =>
    request<EnergyResponse>(`/api/runs/${encodeURIComponent(run_id)}/energy`),
  getRunLedger: (run_id: string) =>
    request<LedgerResponse>(`/api/runs/${encodeURIComponent(run_id)}/ledger`),
  getRunForensicsTimeline: (run_id: string) =>
    request<ForensicsTimeline>(`/api/runs/${encodeURIComponent(run_id)}/forensics-timeline`),

  // ---------- Trust surfaces (ADR-0173 / ADR-0174, read-only) ----------
  getRunTrustRadar: (run_id: string) =>
    request<TrustRadar>(`/api/runs/${encodeURIComponent(run_id)}/trust-radar`),
  getRunRedactionXray: (run_id: string) =>
    request<XRayReport>(`/api/runs/${encodeURIComponent(run_id)}/redaction-xray`),
  // P6 cost trio — pure POST compute endpoints (nova cost attribute/fairness/usage-breakdown).
  costAttribute: (body: { runs: unknown[]; productive_statuses?: string[] }) =>
    postJson<SpendAttribution>('/api/cost/attribute', body),
  costFairness: (body: { totals: Record<string, Record<string, number>> }) =>
    postJson<FairnessReport>('/api/cost/fairness', body),
  costUsageBreakdown: (body: { usage_totals: Record<string, unknown> }) =>
    postJson<UsageBreakdown>('/api/cost/usage-breakdown', body),
  getRunSafetyCase: (run_id: string, template = 'clymer-generic-v0') =>
    request<SafetyCaseResponse>(
      `/api/runs/${encodeURIComponent(run_id)}/safety-case?template=${encodeURIComponent(template)}`,
    ),
  listAssets: (q: { type?: string; status?: string; limit?: number; offset?: number } = {}) =>
    request<{ count: number; total: number; has_more: boolean; limit: number; offset: number; assets: AssetSummary[] }>('/api/assets', q as Record<string, unknown>),
  getStats: () =>
    request<StatsResult>('/api/stats'),
  getAsset: (id: string) =>
    request<AssetDetail>(`/api/assets/${encodeURIComponent(id)}`),
  provenance: (ref: string, depth = 5, edge_type?: string) =>
    request<{ ref: string; depth: number; ancestors: LineageRecord[] }>(
      `/api/lineage/provenance/${encodeURIComponent(ref)}`,
      edge_type && edge_type !== 'all' ? { depth, edge_type } : { depth },
    ),
  blastRadius: (ref: string, depth = 5, edge_type?: string) =>
    request<{ ref: string; depth: number; descendants: LineageRecord[] }>(
      `/api/lineage/blast-radius/${encodeURIComponent(ref)}`,
      edge_type && edge_type !== 'all' ? { depth, edge_type } : { depth },
    ),
  replayChain: (run_id: string) =>
    request<{ run_id: string; chain: LineageRecord[] }>(`/api/lineage/replay-chain/${encodeURIComponent(run_id)}`),
  edges: (limit = 100) => request<{ count: number; edges: LineageEdgePayload[] }>('/api/lineage/edges', { limit: String(limit) }),
  diff: (run_a: string, run_b: string) =>
    request<unknown>('/api/diff', { run_a, run_b }),

  // ---------- Layer B mutations (per ADR-0027 §1) ----------
  registerAsset: (spec_yaml: string, confirmed = true) =>
    postJson<{ ok: boolean; asset: AssetSummary }>('/api/assets', { spec_yaml, confirmed }),
  evalAsset: (id: string, confirmed = true) =>
    postJson<{ ok: boolean; result: { passed: boolean; suites: Array<{ suite_name: string; passed: boolean; reason?: string }> } }>(
      `/api/assets/${encodeURIComponent(id)}/eval`,
      { confirmed },
    ),
  promoteAsset: (id: string, to_status: string, force = false, confirmed = true) =>
    postJson<{ ok: boolean; asset: AssetSummary }>(
      `/api/assets/${encodeURIComponent(id)}/promote`,
      { to_status, force, actor: 'dashboard', confirmed },
    ),
  exportEvidence: (run_id: string, opts: { output_path?: string; key_path?: string; allow_unsafe_skips?: boolean } = {}, confirmed = true) =>
    postJson<{ ok: boolean; bundle_path: string; size_bytes: number; key_autogenerated?: boolean }>(
      `/api/evidence/${encodeURIComponent(run_id)}`,
      { ...opts, confirmed },
    ),
  forensicReplay: (run_id: string, confirmed = true) =>
    postJson<{ ok: boolean; result: Record<string, unknown> }>(
      `/api/runs/${encodeURIComponent(run_id)}/replay/forensic`,
      { confirmed },
    ),
  dryRunReplay: (run_id: string, confirmed = true) =>
    postJson<{ ok: boolean; result: Record<string, unknown> }>(
      `/api/runs/${encodeURIComponent(run_id)}/replay/dry-run`,
      { confirmed },
    ),
  semanticReplay: (run_id: string, confirmed = true) =>
    postJson<{ ok: boolean; result: Record<string, unknown> }>(
      `/api/runs/${encodeURIComponent(run_id)}/replay/semantic`,
      { confirmed },
    ),
  exactReplay: (run_id: string, confirmed = true) =>
    postJson<{ ok: boolean; result: Record<string, unknown> }>(
      `/api/runs/${encodeURIComponent(run_id)}/replay/exact`,
      { confirmed },
    ),
  redact: (run_id: string, confirmed = true) =>
    postJson<{ ok: boolean; findings_count: number; proof_path: string }>(
      `/api/runs/${encodeURIComponent(run_id)}/redact`,
      { confirmed },
    ),
  getRedactionProof: (run_id: string) =>
    request<RedactionProof>(`/api/runs/${encodeURIComponent(run_id)}/redaction-proof`),

  validateRun: (runId: string) =>
    postJson<{ valid: boolean; errors: string[]; run_id: string }>(
      `/api/runs/${encodeURIComponent(runId)}/validate`,
      {},
    ),

  // ---------- asset spec diff (DC-6) ----------
  getAssetDiff: (name: string, fromVersion: string, toVersion: string) =>
    request<AssetDiffResult>(`/api/assets/${encodeURIComponent(name)}/diff`, {
      from_version: fromVersion,
      to_version: toVersion,
    }),

  // ---------- audit log ----------
  audit: (limit = 200, opts?: { cursor?: number; action?: string }) =>
    request<{ count: number; entries: Array<Record<string, unknown>>; next_cursor: number | null }>('/api/audit', {
      limit,
      ...(opts?.cursor != null ? { cursor: opts.cursor } : {}),
      ...(opts?.action ? { action: opts.action } : {}),
    }),

  // ---------- evidence bundles (GET — read-only) ----------
  listEvidence: () =>
    request<{ bundles: EvidenceSummary[]; count: number; total?: number; truncated?: boolean }>(
      '/api/evidence',
    ),
  getEvidenceDetail: (bundle_id: string) =>
    request<EvidenceDetail>(`/api/evidence/${encodeURIComponent(bundle_id)}`),
  verifyEvidence: (bundle_id: string) =>
    postJson<VerifyResult>(`/api/evidence/${encodeURIComponent(bundle_id)}/verify`, {}),

  // ---------- eval history (v0.9 — ADR-0038) ----------
  getEvalHistory: (assetId: string, limit = 20) =>
    request<EvalHistoryResponse>(`/api/assets/${encodeURIComponent(assetId)}/eval-history`, { limit }),

  // ---------- legal holds (DC-2 — ADR-0031) ----------
  listHolds: () =>
    request<HoldsListResult>('/api/holds'),
  createHold: (registry: string, reason: string, duration_days?: number | null) =>
    postJson<HoldRecord>('/api/holds', { registry, reason, duration_days: duration_days ?? null }),
  releaseHold: (hold_id: string) =>
    postJson<{ released: boolean; hold_id: string; registry: string }>(
      `/api/holds/${encodeURIComponent(hold_id)}/release`,
      {},
    ),

  // ---------- policy check (DC-5) ----------
  checkPolicy: (input: PolicyInput, explain = false, policySource?: string) =>
    postJson<PolicyDecision>('/api/policy/check', {
      ...input,
      explain,
      ...(policySource ? { policy_source: policySource } : {}),
    }),

  // ---------- asset suggestion engine (C-2) ----------
  getSuggestions: (capsuleLimit = 10) =>
    request<SuggestionsResult>('/api/runs/suggest-register', { capsule_limit: capsuleLimit }),

  // ---------- time-travel lineage (DD-6) ----------
  timeTravelLineage: (ref: string, at: string, depth = 5) =>
    request<{ ref: string; at: string; depth: number; supported: boolean; ancestors: LineageRecord[] }>(
      `/api/lineage/time-travel/${encodeURIComponent(ref)}`,
      { at, depth },
    ),

  // ---------- DD-7: object capsule store browser ----------
  storageStats: () =>
    request<{
      configured: boolean;
      backend_type: string;
      total_chunks?: number | null;
      total_size_bytes?: number | null;
      worm_score?: number | null;
      manifest_chain_head?: string | null;
      last_put_p99_ms?: number | null;
      error?: string;
    }>('/api/storage/stats'),
  storageManifestChain: (limit = 20) =>
    request<{
      configured: boolean;
      entries: Array<{ hash: string; run_id: string; timestamp: string; size_bytes: number }>;
      error?: string;
    }>('/api/storage/manifest-chain', { limit }),

  // ---------- DD-2: collector status ----------
  collectorStatus: () =>
    request<{
      detected: boolean;
      spool_lag?: number | null;
      signing_p99_ms?: number | null;
      events_per_sec?: number | null;
      last_heartbeat?: string | null;
      collector_version?: string;
      source?: string;
    }>('/api/infra/collector'),
  // ---------- P7: backup-set status listing (NOVA_BACKUP_DIR) ----------
  backupStatus: () =>
    request<{
      detected: boolean;
      reason?: string;
      directory?: string;
      count?: number;
      truncated?: boolean;
      backups?: Array<{
        filename: string;
        ok: boolean;
        set_id: string | null;
        created_at: string | null;
        profile: string | null;
        nova_version: string | null;
        member_count: number | null;
        member_bytes: number | null;
        archive_bytes: number | null;
        signing_status: string | null;
        error: string | null;
      }>;
    }>('/api/infra/backups'),

  // ---------- rollback (DD-3) ----------
  rollbackAsset: (name: string, reason = '', confirmed = true) =>
    postJson<{ ok: boolean; result: { archived_version: string; restored_version: string; rollback_id: string; skipped_archived: boolean } }>(
      `/api/assets/${encodeURIComponent(name)}/rollback`,
      { reason, actor: 'dashboard', confirmed },
    ),

  // ---------- approvals (DD-4) ----------
  getApprovals: (assetId: string) =>
    request<{ asset_id: string; approvals: Array<{ role: string; actor: string; note: string; approved_at: string }>; required: number; supported: boolean }>(
      `/api/assets/${encodeURIComponent(assetId)}/approvals`,
    ),
  approveAsset: (assetId: string, role: string, note = '', confirmed = true) =>
    postJson<{ ok: boolean; asset_id: string; role: string; result: Record<string, unknown> }>(
      `/api/assets/${encodeURIComponent(assetId)}/approve`,
      { role, note, actor: 'dashboard', confirmed },
    ),

  // ---------- admin: token + role management (DD-8) ----------
  listTokens: () =>
    request<TokensListResult>('/api/admin/tokens'),
  issueToken: (label: string, confirmed = true) =>
    postJson<IssueTokenResult>('/api/admin/tokens', { label, confirmed }),
  revokeToken: (fingerprint: string) =>
    deleteRequest<RevokeTokenResult>(`/api/admin/tokens/${encodeURIComponent(fingerprint)}`),
  listRoles: () =>
    request<RolesListResult>('/api/admin/roles'),

  // ---------- admin: API keys read (ADR-0193) ----------
  adminApiKeys: () =>
    request<AdminApiKeysResult>('/api/admin/api-keys'),

  // ---------- operational alerts (ADR-0192) ----------
  alertsRecent: (limit = 50) =>
    request<AlertsRecentResult>('/api/alerts/recent', { limit }),

  // ---------- NovaSeal maker-checker (ADR-0059) ----------
  sealGetPolicy: () =>
    request<SealPolicyResponse>('/api/seal/policy'),
  sealListProposals: (capsuleId: string) =>
    request<SealProposalSummary[]>(`/api/seal/${encodeURIComponent(capsuleId)}/proposals`),
  sealVerify: (capsuleId: string) =>
    postJson<SealVerifyResponse>(`/api/seal/${encodeURIComponent(capsuleId)}/verify`, {}),
  sealLogVerify: (capsuleId?: string) =>
    request<{ consistent: boolean; entry_count: number; message: string; capsule_included?: boolean; errors?: string[] }>(
      '/api/seal/log/verify',
      capsuleId ? { capsule_id: capsuleId } : {},
    ),
  sealBypass: (capsuleId: string, req: SealBypassRequest) =>
    postJson<SealBypassResponse>(`/api/seal/${encodeURIComponent(capsuleId)}/bypass`, req),

  // ---------- Sigstore keyless signing / verification (v0.44.0 / ADR-0071) ----------
  sigstoreSign: (capsule_id: string, manifest_json?: string) =>
    postJson<{
      ok: boolean; capsule_id: string; bundle_path: string;
      log_index: number | null; identity: string | null; note: string;
    }>('/api/seal/sigstore/sign', { capsule_id, ...(manifest_json ? { manifest_json } : {}) }),

  sigstoreVerify: (capsule_id: string) =>
    postJson<{
      ok: boolean; capsule_id: string; valid: boolean;
      identity: string | null; rekor_log_index: number | null; error: string | null;
    }>('/api/seal/sigstore/verify', { capsule_id }),

  // ---------- Tool permission events (cap-004) ----------
  getToolPermissionEvents: (run_id: string) =>
    request<{ run_id: string; events: ToolPermissionEventRecord[] }>(`/api/runs/${encodeURIComponent(run_id)}/tool-permission-events`),

  // ---------- Compliance exports (ADR-0054 / ADR-0055 / ADR-0057) ----------
  exportAnnexIV: (run_id: string, deployment_id: string) =>
    request<Record<string, unknown>>('/api/compliance/annex-iv', { run_id, deployment_id }),
  exportNis2: (run_id: string, incident_id: string, phase?: number) =>
    request<Record<string, unknown>>('/api/compliance/nis2', phase ? { run_id, incident_id, phase } : { run_id, incident_id }),
  subjectProof: (subject_id: string) =>
    request<Record<string, unknown>>('/api/compliance/subject-proof', { subject_id }),

  // ---------- Governance: risk classification (v0.16 — ADR-0056) ----------
  classifyFromCapsule: (run_id: string, vocabulary = 'eu-ai-act/2024.1.0') =>
    request<{ run_id: string; vocabulary: string; result: Record<string, unknown>; note: string }>(
      '/api/governance/classify', { run_id, vocabulary }
    ),

  // ---------- Compliance audit (v0.16 — ADR-0061) ----------
  auditMap: (profile: string) =>
    request<{ profile: string; profile_name: string; framework: string; control_count: number; checkers: Array<Record<string, unknown>> }>(
      '/api/compliance/audit/map', { profile }
    ),
  auditReport: (run_id: string, profile: string) =>
    postJson<Record<string, unknown>>('/api/compliance/audit/report', { run_id, profile }),

  // ---------- Examiner export (v0.16 — ADR-0061/0062) ----------
  exportExaminer: (run_id: string, format: 'bagit' | 'pccp' | 'iso42001') =>
    postJson<{ ok: boolean; format: string; run_id: string; output_path: string; size_bytes: number; note: string }>(
      `/api/compliance/examiner/${format}`, { run_id }
    ),

  // ---------- DB-KG-1: Capsule Knowledge Graph (v0.18.0 — ADR-0067) ----------
  kgStatus: () =>
    request<{ store: string; store_health: string; db_path: string; edge_count: number; node_counts?: Record<string, number>; note?: string }>(
      '/api/kg/status'
    ),
  kgTopology: (max_nodes = 500) =>
    request<{
      ok: boolean;
      nodes: Array<{ id: string; type: string; name?: string; provider?: string; url?: string }>;
      edges: Array<{ src: string; src_type: string; dst: string; dst_type: string; edge_type: string; call_count: number; confidence: number }>;
      node_counts: Record<string, number>;
      edge_counts: Record<string, number>;
      truncated?: boolean;
      truncated_reason?: string[];
      note?: string;
      error?: string;
    }>('/api/kg/topology', { max_nodes }),
  kgAgentEdges: (agent_id: string) =>
    request<{
      agent_id: string;
      models: Array<{ model_id: string; call_count: number; verified_count: number; confidence: number }>;
      tools: Array<{ tool_id: string; call_count: number; verified_count: number; confidence: number }>;
      mcp_servers: Array<{ server_id: string; server_name: string; call_count: number }>;
    }>(`/api/kg/agents/${encodeURIComponent(agent_id)}/edges`),

  // ---------- DB-CAP-1: Capture-level policy (v0.18.0 — cap-004) ----------
  captureLevelGet: () =>
    request<{ current_level: string; env_var: string; tiers: Record<string, string[]>; note: string }>(
      '/api/policy/capture-level'
    ),
  captureLevelSet: (level: string) =>
    postJson<{ ok: boolean; level: string; instruction: string; note: string }>(
      '/api/policy/capture-level', { level }
    ),

  // ---------- DB-ERA-1: GDPR erasure (real execution per ADR-0210 — experimental) ----------
  erasureRequest: (subject_id: string, reason: string = 'gdpr_art_17', confirmed = true) =>
    postJson<{ ok: boolean; cap003_enabled: boolean; reattached: boolean; request: { request_id: string; subject_sha256: string; state: string; reason: string; requested_at: string; executed_at: string | null; capsule_ids: string[]; receipt: Record<string, unknown> | null; receipt_sha256: string | null; error_class: string | null; error_detail: string | null } }>(
      '/api/compliance/erasure/request', { subject_id, reason, confirmed }
    ),
  erasureStatus: (subject_id?: string) =>
    request<{ cap003_enabled: boolean; requests: Array<Record<string, unknown>> }>(
      '/api/compliance/erasure/status', subject_id ? { subject_id } : {}
    ),

  // ---------- DB-STG-1: Storage operations (v0.18.0 — cap-009/003) ----------
  storageValidate: (endpoint?: string, bucket: string = 'nova-capsules') =>
    request<{ ok: boolean; endpoint: string | null; bucket: string; result?: Record<string, unknown>; error?: string; error_class?: string }>(
      '/api/storage/validate', { ...(endpoint ? { endpoint } : {}), bucket }
    ),
  storageInspect: (run_id: string) =>
    request<{ run_id: string; audit_object_key: string; pii_object_key: string | null; cap003_enabled: boolean; note: string }>(
      `/api/storage/inspect/${encodeURIComponent(run_id)}`
    ),

  capsuleVerify: (runId: string) =>
    postJson<CapsuleVerifyResult>(`/api/runs/${encodeURIComponent(runId)}/verify`, {}),

  lineageEmitOpenLineage: (runId: string) =>
    request<OpenLineageExportResult>(`/api/lineage/${encodeURIComponent(runId)}/emit-openlineage`),

  registerFromSuggestion: (specYaml: string) =>
    postJson<{ ok: boolean; name?: string; error?: string }>('/api/assets/register-from-yaml', { spec_yaml: specYaml }),

  // ---------- DB-COST-1: Cost report (v0.19.0 — cap-002) ----------
  costPricing: () =>
    request<{ pricing: Array<{ model: string; input_price_per_1k_usd: number; output_price_per_1k_usd: number }>; source: string; note: string }>(
      '/api/cost/pricing'
    ),
  costReport: (params: { run_id?: string; days?: number } = {}) =>
    request<{
      ok: boolean;
      backend: string;
      reason?: string;
      clickhouse_url?: string;
      run_id: string | null;
      days: number;
      totals: { input_tokens: number; completion_tokens: number; cost_usd: number };
      by_model: Array<Record<string, unknown>>;
      note: string;
    }>('/api/cost/report', params as Record<string, unknown>),

  // ---------- DB-SCH-1: Schema list (v0.19.0 — cap-001) ----------
  schemaList: () =>
    request<{
      schema_version: string;
      spec_path: string;
      event_types: string[];
      event_type_count: number;
      categories: Record<string, string[]>;
      pydantic_models: Array<{ name: string; fields: string[]; description: string }>;
      note: string;
    }>('/api/schema/list'),

  // ---------- v0.19.0: KG init / ingest ----------
  kgInit: () =>
    postJson<{ ok: boolean; db_path: string; note: string; error?: string }>(
      '/api/kg/init', {}
    ),
  kgIngest: (capsule_path: string, verified = false) =>
    postJson<{ ok: boolean; ingested: number; skipped: number; written: number; capsule_path: string; kg_path: string; note: string; error?: string }>(
      '/api/kg/ingest', { capsule_path, verified }
    ),
  kgIngestAll: (capsule_base?: string) =>
    postJson<{ ok: boolean; total: number; newly_ingested: number; skipped: number; failed: number; capsule_base?: string; error?: string }>(
      '/api/kg/ingest-all', capsule_base ? { capsule_base } : {}
    ),
  kgQuery: (agent_id: string) =>
    request<{ ok: boolean; agent_id: string; models: string[]; tools: string[]; error?: string }>(
      '/v1/kg/query', { agent_id }
    ),
  kgAudit: () =>
    request<{
      ok: boolean;
      store_health?: string;
      node_count?: number;
      edge_count?: number;
      orphaned_edges?: number;
      zero_call_count?: number;
      issues?: string[];
      error?: string;
    }>('/v1/kg/audit'),
  kgEntityQueueList: () =>
    request<{ ok: boolean; count: number; items: Array<Record<string, unknown>> }>(
      '/api/kg/entity-queue'
    ),
  kgEntityQueueStats: () =>
    request<{ ok: boolean; pending: number; approved: number; rejected: number }>(
      '/api/kg/entity-queue/stats'
    ),
  kgEntityQueueApprove: (item_id: string, canonical: string, resolved_by = 'dashboard') =>
    postJson<{ ok: boolean; item_id: string; canonical: string }>(
      `/api/kg/entity-queue/${encodeURIComponent(item_id)}/approve`, { canonical, resolved_by }
    ),
  kgEntityQueueReject: (item_id: string, resolved_by = 'dashboard') =>
    postJson<{ ok: boolean; item_id: string; status: string }>(
      `/api/kg/entity-queue/${encodeURIComponent(item_id)}/reject`, { resolved_by }
    ),
  kgAliasList: (canonical?: string) =>
    request<{
      ok: boolean;
      count: number;
      aliases: Array<{ alias: string; canonical: string; entity_type: string; confidence: number; source: string; created_at: string }>;
    }>('/api/kg/aliases', canonical ? { canonical } : {}),
  kgAliasRegister: (alias: string, canonical: string, entity_type: string, registered_by = 'dashboard') =>
    postJson<{ ok: boolean; alias: string; canonical: string; entity_type: string; error?: string }>(
      '/api/kg/aliases', { alias, canonical, entity_type, registered_by }
    ),

  // ---------- v0.19.0: run utilities ----------
  newRunId: () =>
    request<{ run_id: string; env_var: string; cli_cmd: string; note: string }>(
      '/api/admin/new-run-id'
    ),
  getRunChildren: (runId: string) =>
    request<{ run_id: string; status: string | null; exit_code: number | null; child_count: number; children: Array<{ run_id: string; status: string | null; edge_type: string | null; exit_code: number | null }> }>(
      `/api/runs/${encodeURIComponent(runId)}/children`
    ),
  validateDistributed: (runId: string) =>
    postJson<{ ok: boolean; status: string; message: string; exit_code: number; run_id: string }>(
      `/api/runs/${encodeURIComponent(runId)}/validate-distributed`, {}
    ),

  // ---------- v0.20.0: unregister, doctor, policy test/explain, audit extensions ----------
  unregisterAsset: (name: string, version: string, force = false) =>
    deleteRequest<{ ok: boolean; name: string; version: string; snapshot: Record<string, unknown> }>(
      `/api/assets/${encodeURIComponent(name)}/${encodeURIComponent(version)}`,
      force ? { force: 'true' } : {},
    ),

  doctor: () =>
    request<{ ok: boolean; checks: Array<{ name: string; ok: boolean; detail: string; db_path?: string }> }>(
      '/api/doctor'
    ),

  policyTest: (bundlePath?: string) =>
    postJson<{ ok: boolean; exit_code?: number; output: string; bundle_path?: string; backend?: string; reason?: string }>(
      '/api/policy/test', bundlePath ? { bundle_path: bundlePath } : {}
    ),

  policyExplain: (decisionId: string) =>
    request<{ ok: boolean; decision_id: string; entries: Array<Record<string, unknown>>; count: number }>(
      '/api/policy/explain', { decision_id: decisionId }
    ),

  policyRecentDecisions: (limit = 50) =>
    request<{ decision_ids: string[] }>('/api/policy/recent-decisions', { limit }),

  auditCoverage: (profile = 'nist-ai-rmf', threshold = 0.8) =>
    request<{
      profile: string; overall_score: number; threshold: number; threshold_met: boolean;
      capsule_count: number; total_controls: number; covered_controls: number;
      partial_controls: number; missing_controls: number;
      coverages: Array<Record<string, unknown>>;
    }>('/api/compliance/audit/coverage', { profile, threshold: String(threshold) }),

  auditBundle: (profile = 'nist-ai-rmf') =>
    postJson<{ ok: boolean; profile: string; report_id: string; overall_score: number; filename: string; content_base64: string; size_bytes: number }>(
      '/api/compliance/audit/bundle', { profile }
    ),

  auditVerify: (report: Record<string, unknown>) =>
    postJson<{ ok: boolean; valid: boolean; report_id?: string; profile?: string; overall_score?: number; errors: string[] }>(
      '/api/compliance/audit/verify', { report }
    ),

  // ---------- v0.26.0: OWASP assurance, MCP scanner, adapter registry ----------
  assureRun: (runId: string, capsulePath?: string) =>
    request<{
      run_id?: string;
      overall_status?: string;
      pass_count?: number;
      fail_count?: number;
      warn_count?: number;
      skip_count?: number;
      results?: Array<{ check_id: string; category: string; status: string; message: string }>;
      ok?: boolean;
      backend?: string;
      reason?: string;
    }>(
      `/api/assure/${encodeURIComponent(runId)}`,
      capsulePath ? { capsule_path: capsulePath } : {},
    ),

  mcpScan: (manifest: Record<string, unknown>, threshold = 'HIGH') =>
    postJson<{
      server_name: string;
      overall_risk_level: string;
      total_findings: number;
      tools: Array<{
        tool_name: string;
        risk_score: number;
        highest_severity: string;
        findings: Array<{ category: string; severity: string; rule_id: string; message: string; evidence: string; tool_name: string }>;
      }>;
    }>('/api/mcp/scan', { manifest, threshold }),

  mcpRiskReport: (manifest: Record<string, unknown>) =>
    postJson<{
      ok: boolean;
      server_name?: string;
      overall_risk_level?: string;
      total_findings?: number;
      tools?: Array<{
        tool_name: string;
        risk_score: number;
        highest_severity: string;
        findings: Array<{ category: string; severity: string; rule_id: string; message: string; evidence: string }>;
      }>;
      note?: string;
    }>('/api/mcp/risk-report', { manifest }),

  listAdapters: () =>
    request<{
      adapters: Array<{ id: string; module: string; function: string; framework: string; extra: string; available: boolean }>;
      count: number;
    }>('/api/adapters'),

  validateSpec: (spec_yaml: string) =>
    postJson<{ ok: boolean; valid: boolean; errors: string[]; note: string }>(
      '/api/validate-spec', { spec_yaml }
    ),

  assetReport: (format: 'markdown' | 'json' = 'json') =>
    request<{ ok: boolean; format: string; content: string; note: string }>(
      '/api/report', { format }
    ),

  // ---------- v0.27.0: capsule delete ----------
  deleteRun: (run_id: string, force = false) =>
    deleteRequest<{ ok: boolean; run_id: string; note: string }>(
      `/api/runs/${encodeURIComponent(run_id)}`,
      force ? { force: 'true' } : {},
    ),

  // ---------- v0.27.0: admin completions (assign/revoke-role, flush-jwks, db-upgrade, capsule-migrate) ----------
  assignRole: (subject: string, role: string) =>
    postJson<{ ok: boolean; subject: string; role: string; assigned_by?: string }>(
      '/api/admin/roles', { subject, role }
    ),

  revokeRole: (subject: string, role: string) =>
    deleteRequest<{ ok: boolean; subject: string; role: string }>(
      `/api/admin/roles/${encodeURIComponent(subject)}/${encodeURIComponent(role)}`, {}
    ),

  flushJwksCache: () =>
    postJson<{ ok: boolean; note: string }>('/api/admin/flush-jwks-cache', {}),

  dbUpgrade: (revision = 'head') =>
    postJson<{ ok: boolean; revision: string; output?: string; note: string }>(
      '/api/db/upgrade', { revision }
    ),

  capsuleMigrate: (source: string, output: string) =>
    postJson<{
      ok: boolean;
      source: string;
      output: string;
      files_migrated?: number;
      files_updated?: string[];
      lineage_edge_id?: string;
      note: string;
    }>('/api/capsule-migrate', { source, output }),

  // ---------- v0.27.0: lineage import + eval compare ----------
  lineageImport: (capsule_path: string) =>
    postJson<{ ok: boolean; capsule_path: string; imported: number; skipped: number; file_count: number; note: string }>(
      '/api/lineage/import', { capsule_path }
    ),

  evalCompare: (baseline_json: string, candidate_json: string, alpha = 0.05, min_samples = 5) =>
    postJson<{
      ok: boolean;
      suite_id?: string;
      regression_detected?: boolean;
      summary?: string;
      metrics?: Array<{ name: string; baseline_value: number; candidate_value: number; delta: number; relative_delta: number; p_value: number | null; significant: boolean }>;
    }>('/api/eval/compare', { baseline_json, candidate_json, alpha, min_samples }),

  // ---------- v0.34.0: EU AI Act Art.12 ----------
  euaiactStatus: () =>
    request<{ ok: boolean; high_risk: boolean; provider_mode: boolean; retention_months: number; deadline: string; note: string }>(
      '/api/compliance/euaiact/status'
    ),

  euaiactExport: (from_date?: string, to_date?: string) =>
    postJson<{ ok: boolean; records: Array<{ run_id: string; created_at: string; status: string; logging_event_type: string[] }>; count: number; retention_months: number; mode: string }>(
      '/api/compliance/euaiact/export',
      { ...(from_date ? { from_date } : {}), ...(to_date ? { to_date } : {}) }
    ),

  // ---------- v0.34.0: RO-Crate export ----------
  exportRoCrate: (run_id: string) =>
    postJson<{ ok: boolean; run_id: string; filename: string; zip_base64: string; size_bytes: number; note: string }>(
      '/api/compliance/export/rocrate', { run_id }
    ),

  // ---------- v0.34.0: PROV-JSON lineage export ----------
  exportProvJson: (run_id: string) =>
    postJson<{ ok: boolean; run_id: string; document: object; note: string }>(
      '/api/lineage/export-prov', { run_id }
    ),

  // ---------- v0.34.0: C2PA manifest export ----------
  exportC2pa: (run_id: string, include_training_mining = false) =>
    postJson<{ ok: boolean; run_id: string; manifest: object; note: string }>(
      '/api/compliance/export/c2pa', { run_id, include_training_mining }
    ),

  // ---------- GDPR RoPA export (cap-007) ----------
  exportRopa: (run_id: string, controller_name = '', controller_contact = '') =>
    postJson<{
      ok: boolean; run_id: string; document: object;
      completeness: string; missing_fields: string[]; note: string;
    }>(
      '/api/compliance/export/ropa', { run_id, controller_name, controller_contact }
    ),

  // ---------- AI-SBOM export (cap-008, CycloneDX 1.6) ----------
  exportAibom: (run_id: string) =>
    postJson<{
      ok: boolean; run_id: string; bom_format: string; serial_number: string;
      component_count: number;
      components: Array<{ type: string; name: string; version: string; description: string; licenses: object[]; properties: object[] }>;
      generated_at: string; note: string;
    }>(
      '/api/compliance/export/aibom', { run_id }
    ),

  // ---------- NIST AI RMF export (cap-009) ----------
  exportNistRmf: (run_id: string) =>
    postJson<{
      ok: boolean; run_id: string; overall_score: number | null; risk_level: string;
      metrics: Array<{ function: string; category: string; metric_id: string; description: string; score: number | null; evidence: string | null }>;
      missing_evidence: string[]; generated_at: string; note: string;
    }>(
      '/api/compliance/export/nist-rmf', { run_id }
    ),

  // ---------- AIBOM status (nova aibom status — cap-008) ----------
  aibomStatus: () =>
    request<{
      ok: boolean; regulation: string; cra_deadline: string; spec_version: string;
      capsule_directory: string; total_capsules: number; capsules_with_aibom: number;
      capsules_missing_aibom: number; coverage_status: 'no_capsules' | 'partial' | 'complete'; note: string;
    }>('/api/aibom/status'),

  // ---------- PII DEK crypto-shredding (nova pii erase — ADR-0069) ----------
  piiErase: (subject_id: string, capsule_ids: string[] = [], retention_months = 6) =>
    postJson<{
      ok: boolean; subject_id: string; erased: boolean;
      receipt: object; note: string; error?: string;
    }>('/api/compliance/pii/erase', { subject_id, capsule_ids, retention_months }),

  // ---------- HIPAA Safe Harbor proof (nova export-hipaa-proof) ----------
  exportHipaaProof: (run_id: string) =>
    postJson<{
      ok: boolean; run_id: string; proof_digest: string; disclaimer: string;
      assessed_at: string; identifier_count: number;
      categories: Array<{ id: string; name: string; status: string; evidence_source: string | null }>;
    }>('/api/compliance/export/hipaa-proof', { run_id }),

  // ---------- v0.46.0 dashboard parity gap closure (12 CLI capabilities) ----------

  // nova eval list
  evalSuites: () =>
    request<{
      ok: boolean; entry_point_group: string;
      suites: Array<{ suite_id: string; version: string | null; oci_digest: string | null; entry_point: string; error?: string }>;
    }>('/api/eval/suites'),

  // nova eval run
  evalRun: (run_id: string, suite: string, config: Record<string, string> = {}) =>
    postJson<{
      ok: boolean;
      result: {
        suite_id: string; suite_version: string; oci_digest: string | null;
        capsule_id: string; passed: boolean;
        metrics: Array<{ name: string; value: number; threshold: number; unit: string; passed: boolean }>;
      };
    }>('/api/eval/run', { run_id, suite, config }),

  // nova policy list
  policyList: (namespace?: string) =>
    request<{
      ok: boolean; bundle_path: string;
      rego_files: Array<{ file: string; size_bytes: number }>;
      policy_db: string;
      signed_policies: Array<{ version: number; namespace: string; created_at: string }>;
    }>('/api/policy/list', namespace ? { namespace } : {}),

  // nova policy sign
  policySign: (args: {
    key_path: string; cert_path: string;
    proposer_subjects: string[]; approver_subjects: string[];
    bypass_valid_hours?: number; db_path?: string;
  }) =>
    postJson<{
      ok: boolean; version: number; proposers: string[]; approvers: string[];
      bypass_valid_hours: number; policy_db: string;
    }>('/api/policy/sign', args),

  // nova classify list-vocabularies
  governanceVocabularies: () =>
    request<{
      ok: boolean;
      vocabularies: Array<{ framework: string; version: string; reference: string; path: string }>;
    }>('/api/governance/vocabularies'),

  // nova classify run (manual record)
  classifyManual: (args: {
    name: string; description: string; use_case_domain: string; deployment_context: string;
    uses_biometrics?: boolean; affects_fundamental_rights?: boolean; is_general_purpose?: boolean;
  }) =>
    postJson<{ ok: boolean; classification: Record<string, unknown> }>(
      '/api/governance/classify-manual', args
    ),

  // nova aibom generate [--all] [--force]
  aibomGenerate: (args: { run_id?: string; all?: boolean; force?: boolean }) =>
    postJson<{
      ok: boolean; written: number; skipped: number; failed: number;
      errors?: string[]; path?: string;
    }>('/api/aibom/generate', args),

  // nova ingest-capsule <id> | --all
  ingestCapsule: (args: { run_id?: string; all?: boolean }) =>
    postJson<{ ok: boolean; indexed?: number; found?: boolean; is_new?: boolean }>(
      '/api/ingest-capsule', args
    ),

  // nova run show --with-children
  runTree: (run_id: string) =>
    request<{
      ok: boolean; root: CapsuleTreeNode; total_nodes: number; orphans: CapsuleTreeNode[];
    }>(`/api/runs/${encodeURIComponent(run_id)}/tree`),

  // nova run lineage [--edge-types]
  runSpoolLineage: (run_id: string, edge_types?: string) =>
    request<{
      ok: boolean; run_id: string; edge_types_filter: string[] | null;
      edges: Array<Record<string, unknown>>; count: number;
    }>(`/api/runs/${encodeURIComponent(run_id)}/run-lineage`, edge_types ? { edge_types } : {}),

  // nova lineage-store profile
  lineageStoreProfile: (args: { target?: string; node_size?: string; rf?: number; image_tag?: string } = {}) =>
    request<{ ok: boolean; target: string; profile_yaml: string }>(
      '/api/lineage-store/profile',
      Object.fromEntries(
        Object.entries(args).filter(([, v]) => v !== undefined && v !== '')
      ) as Record<string, string | number>
    ),

  // nova scan-secrets [--fail-on]
  scanSecrets: (run_id: string, fail_on?: string) =>
    request<{
      ok: boolean; run_id: string;
      findings_count: { total?: number; by_severity?: Record<string, number> };
      findings: Array<{ rule_id: string; severity: string; target_ref: string; redaction_strategy: string }>;
      fail_on: string | null; triggered: boolean; triggered_count: number;
    }>(`/api/runs/${encodeURIComponent(run_id)}/scan-secrets`, fail_on ? { fail_on } : {}),

  // ========== Dashboard CLI-coverage expansion (v0.55) ==========

  // nova incident open/list/status/export
  incidentList: () =>
    request<{ ok: boolean; count?: number; incidents: IncidentView[]; error?: string }>('/api/incidents'),
  incidentOpen: (args: { title: string; classification: string; severity: string; occurred_at?: string; aware_at?: string; run_ids?: string[] }) =>
    postJson<{ ok: boolean; incident?: IncidentView; error?: string }>('/api/incidents', args),
  incidentGet: (id: string) =>
    request<{ ok: boolean; incident?: IncidentView; error?: string }>(`/api/incidents/${encodeURIComponent(id)}`),
  incidentTransition: (id: string, to_status: string) =>
    postJson<{ ok: boolean; incident?: IncidentView; error?: string }>(`/api/incidents/${encodeURIComponent(id)}/transition`, { to_status }),
  incidentExport: (id: string, fmt: 'aim' | 'nis2' = 'aim') =>
    request<{ ok: boolean; format: string; report: Record<string, unknown>; error?: string }>(`/api/incidents/${encodeURIComponent(id)}/export`, { fmt }),

  // nova diagnose (ADR-0084)
  diagnose: (run_id: string) =>
    request<{ ok: boolean; run_id: string; attribution?: Record<string, unknown>; error?: string }>(
      `/api/runs/${encodeURIComponent(run_id)}/diagnose`,
    ),

  // nova evidence completeness / bind (ADR-0087)
  evidenceCompleteness: (run_id: string) =>
    request<{ ok: boolean; run_id: string; assertion?: Record<string, unknown>; error?: string }>(
      `/api/evidence/completeness/${encodeURIComponent(run_id)}`,
    ),
  evidenceBind: (run_id: string, profile = 'nist-ai-rmf') =>
    postJson<{ ok: boolean; run_id: string; profile: string; count: number; bindings: Array<Record<string, unknown>>; error?: string }>(
      `/api/evidence/${encodeURIComponent(run_id)}/bind`, { profile },
    ),

  // nova seal ratchet init/rotate/status (ADR-0089)
  ratchetStatus: (node_id: string) =>
    request<{ ok: boolean; state?: Record<string, unknown>; registry_epochs?: number[]; error?: string }>(
      '/api/seal/ratchet/status', { node_id },
    ),
  ratchetInit: (node_id: string) =>
    postJson<{ ok: boolean; state?: Record<string, unknown>; error?: string }>('/api/seal/ratchet/init', { node_id }),
  ratchetRotate: (node_id: string) =>
    postJson<{ ok: boolean; state?: Record<string, unknown>; error?: string }>(
      '/api/seal/ratchet/rotate', { node_id, confirmed: true },
    ),

  // nova daemon status (ADR-0092)
  daemonStatus: () =>
    request<{ ok: boolean; alive: boolean; socket_path?: string; error?: string }>('/api/ops/daemon-status'),

  // nova export-system-card (ADR-0085)
  exportSystemCard: (run_id: string) =>
    postJson<{ ok: boolean; run_id: string; card?: Record<string, unknown>; sealed?: Record<string, unknown>; error?: string }>(
      `/api/runs/${encodeURIComponent(run_id)}/export-system-card`, {},
    ),

  // nova rebuild-metadata-db (FR-04) — gated, destructive
  rebuildMetadataDb: (args: { prefix?: string; backend?: string; target_db?: string } = {}) =>
    postJson<{ ok: boolean; target_db?: string; capsules_found?: number; time_to_replay_seconds?: number; integrity_warnings?: string[]; error?: string }>(
      '/api/admin/rebuild-metadata-db', { ...args, confirmed: true },
    ),

  // P8 safe mutation — rebuild the runs_cache index from capsules (idempotent, lossless).
  reindexRuns: () =>
    postJson<{ ok: boolean; reindexed?: number; total?: number; error?: string }>(
      '/api/admin/reindex-runs', { confirmed: true },
    ),
  // P10 safe mutation — re-seed the live topology store from capsules (idempotent merge).
  topologySeed: () =>
    postJson<{ agents_added?: number; edges_added?: number; errors?: string[] }>(
      '/api/topology/seed', {},
    ),

  reports: {
    async fetch(
      type: string,
      params: Record<string, string | undefined>,
      format: 'json' | 'csv' = 'json',
    ): Promise<{ columns: string[]; rows: Record<string, unknown>[]; count: number } | Blob> {
      const { token, base } = getCfg();
      if (!token) throw new ServeApiError(401, 'no token configured — connect first');
      const sp = new URLSearchParams({ token, format });
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== '') sp.set(k, v);
      }
      const res = await fetch(`${base}/api/reports/${type}?${sp.toString()}`);
      if (!res.ok) {
        if (res.status === 401) { clearConnection(); emitAuthChange(); }
        throw new ServeApiError(res.status, `report ${type} failed: ${res.status}`);
      }
      if (format === 'csv') return res.blob();
      return res.json() as Promise<{ columns: string[]; rows: Record<string, unknown>[]; count: number }>;
    },

    /** Report registry catalog: filters + chart specs (ADR-0200/0201). */
    catalog: () =>
      request<{
        reports: {
          report_id: string;
          title: string;
          audience: string;
          filter_keys: string[];
          required_filters: string[];
          chart: {
            kind: 'line' | 'bar' | 'stacked-bar' | 'multi-line';
            x: string;
            y: string[];
            colors: string[];
            title: string;
            requires_filters: string[];
          } | null;
        }[];
      }>('/api/reports/catalog'),

    /** Self-contained HTML/PDF report artifact. 501 = WeasyPrint absent. */
    async export(
      type: string,
      params: Record<string, string | undefined>,
      format: 'html' | 'pdf',
    ): Promise<Blob> {
      const { token, base } = getCfg();
      if (!token) throw new ServeApiError(401, 'no token configured — connect first');
      const sp = new URLSearchParams({ token, format });
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== '') sp.set(k, v);
      }
      const res = await fetch(
        `${base}/api/reports/${encodeURIComponent(type)}/export?${sp.toString()}`,
      );
      if (!res.ok) {
        if (res.status === 401) { clearConnection(); emitAuthChange(); }
        let detail = `report export failed: ${res.status}`;
        try {
          const body = (await res.json()) as { detail?: string };
          if (body.detail) detail = body.detail;
        } catch { /* non-JSON error body */ }
        throw new ServeApiError(res.status, detail);
      }
      return res.blob();
    },
  },
};
