/**
 * Normalisation utilities for raw capsule JSONL data.
 *
 * Raw model-calls.jsonl and tool-calls.jsonl use OTel GenAI semconv key names
 * (e.g. "gen_ai.request.model", "parent_span_id").  These helpers map them to
 * clean typed interfaces consumed by the dashboard components.
 */

// ─── Agent colour palette ─────────────────────────────────────────────────────

const AGENT_PALETTE = [
  '#3b82f6', // blue
  '#10b981', // emerald
  '#f59e0b', // amber
  '#8b5cf6', // violet
  '#ef4444', // red
  '#06b6d4', // cyan
  '#ec4899', // pink
  '#84cc16', // lime
  '#f97316', // orange
  '#a855f7', // purple
];

export function buildAgentColors(agentNames: string[]): Map<string, string> {
  const sorted = [...new Set(agentNames)].sort();
  const map = new Map<string, string>();
  sorted.forEach((name, i) => map.set(name, AGENT_PALETTE[i % AGENT_PALETTE.length]));
  return map;
}

export function agentInitial(name: string): string {
  const words = name.replace(/_/g, ' ').trim().split(/\s+/);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return (name[0] ?? '?').toUpperCase();
}

// ─── Timestamp helpers ────────────────────────────────────────────────────────

export function parseTs(s: string | undefined): number {
  if (!s) return 0;
  const d = new Date(s);
  return isNaN(d.getTime()) ? 0 : d.getTime();
}

// ─── Agent inference from system prompt ──────────────────────────────────────

export function inferAgentFromMessages(
  messages: Array<{ role: string; content: string }>,
): string | undefined {
  const sys = messages.find(m => m.role === 'system')?.content ?? '';
  if (!sys) return undefined;

  // "You are the Mission Commander for …" → "mission_commander"
  // "You are the Kubernetes Sentinel…"   → "kubernetes_sentinel"
  const m = sys.match(/[Yy]ou are(?: the)?\s+([\w][\w ]*?)(?:\s+for|\s+agent|\.|,|\n|$)/);
  if (m) {
    return m[1].trim().toLowerCase().replace(/\s+/g, '_');
  }
  return undefined;
}

// ─── Normalized types ─────────────────────────────────────────────────────────

export interface NormalizedModelCall {
  id: string;
  parentSpanId: string;
  startedAt: string;
  startedMs: number;        // parsed epoch ms
  durationMs: number;
  status: string;           // success | error | timeout | denied | partial

  system: string;           // gen_ai.system  (openai, anthropic, ollama, …)
  model: string;            // gen_ai.request.model
  operation: string;        // gen_ai.operation.name

  messages: Array<{ role: string; content: string }>;
  temperature?: number;
  maxTokens?: number;

  finishReason?: string;
  completionPreview?: string;

  inputTokens?: number;
  outputTokens?: number;

  cacheHit?: boolean;
  cacheKind?: string;

  agent?: string;           // nova.agent.id or inferred from system prompt
  phase?: string;           // nova.phase (if present)

  raw: Record<string, unknown>;
}

export interface NormalizedToolCall {
  id: string;
  parentSpanId: string;
  agentCallId?: string;     // links to model call that triggered this tool
  startedAt: string;
  startedMs: number;
  durationMs: number;
  status: string;           // success | error | timeout | denied

  toolName: string;
  toolVersion?: string;
  transport?: string;       // mcp | http | grpc | shell | python | filesystem

  mutates: boolean;
  mutationClass?: string;

  arguments?: Record<string, unknown>;
  resultText?: string;      // extracted text preview (first 500 chars)
  resultError: boolean;

  mcpServerName?: string;

  agent?: string;           // nova.agent.id

  raw: Record<string, unknown>;
}

// ─── Normalisers ──────────────────────────────────────────────────────────────

export function normalizeModelCall(raw: Record<string, unknown>): NormalizedModelCall {
  const g = (k: string) => raw[k];

  const messages = (g('gen_ai.request.messages') as Array<{ role: string; content: string }>) ?? [];
  const choices = (g('gen_ai.response.choices') as Array<{
    finish_reason?: string;
    message?: { content?: string };
  }>) ?? [];

  const finishReason = choices[0]?.finish_reason;
  const completionText = choices[0]?.message?.content;

  const cache = g('nova.cache') as { hit?: boolean; kind?: string } | undefined;

  const agentExplicit = g('nova.agent.id') as string | undefined;
  const agent = agentExplicit ?? inferAgentFromMessages(messages);

  const startedAt = (g('started_at') as string) ?? '';

  return {
    id: (g('model_call_id') as string) ?? '',
    parentSpanId: (g('parent_span_id') as string) ?? '',
    startedAt,
    startedMs: parseTs(startedAt),
    durationMs: (g('duration_ms') as number) ?? 0,
    status: (g('status') as string) ?? 'unknown',
    system: (g('gen_ai.system') as string) ?? '',
    model: (g('gen_ai.request.model') as string) ?? 'LLM call',
    operation: (g('gen_ai.operation.name') as string) ?? 'chat',
    messages,
    temperature: g('gen_ai.request.temperature') as number | undefined,
    maxTokens: g('gen_ai.request.max_tokens') as number | undefined,
    finishReason,
    completionPreview: completionText ? completionText.slice(0, 500) : undefined,
    inputTokens: g('gen_ai.usage.input_tokens') as number | undefined,
    outputTokens: g('gen_ai.usage.output_tokens') as number | undefined,
    cacheHit: cache?.hit,
    cacheKind: cache?.kind,
    agent,
    phase: g('nova.phase') as string | undefined,
    raw,
  };
}

export function normalizeToolCall(raw: Record<string, unknown>): NormalizedToolCall {
  const g = (k: string) => raw[k];

  // MCP result shape: { content: [{type, text}], isError: bool }
  const result = g('result') as
    | { content?: Array<{ type: string; text: string }>; isError?: boolean }
    | string
    | undefined;

  let resultText: string | undefined;
  let resultError = false;

  if (result && typeof result === 'object') {
    const content = (result as { content?: Array<{ type: string; text: string }> }).content;
    if (Array.isArray(content) && content.length > 0) {
      resultText = String(content[0].text ?? '').slice(0, 500);
    }
    resultError = (result as { isError?: boolean }).isError ?? false;
  } else if (typeof result === 'string') {
    resultText = result.slice(0, 500);
  }

  const mcp = g('mcp') as { server_name?: string } | undefined;
  const agentCallId = g('agent_call_id') as string | undefined;
  const startedAt = (g('started_at') as string) ?? '';

  return {
    id: (g('tool_call_id') as string) ?? '',
    parentSpanId: (g('parent_span_id') as string) ?? '',
    agentCallId: agentCallId ?? undefined,
    startedAt,
    startedMs: parseTs(startedAt),
    durationMs: (g('duration_ms') as number) ?? 0,
    status: (g('status') as string) ?? 'unknown',
    toolName: (g('tool_name') as string) ?? 'tool',
    toolVersion: g('tool_version') as string | undefined,
    transport: g('transport') as string | undefined,
    mutates: (g('mutates') as boolean) ?? false,
    mutationClass: g('mutation_class') as string | undefined,
    arguments: g('arguments') as Record<string, unknown> | undefined,
    resultText,
    resultError,
    mcpServerName: mcp?.server_name,
    agent: g('nova.agent.id') as string | undefined,
    raw,
  };
}

// ─── Agent propagation from model calls to tool calls ────────────────────────

/**
 * For tool calls that have no nova.agent.id, propagate agent from the model
 * call that triggered them (via agent_call_id).
 */
export function propagateAgents(
  modelCalls: NormalizedModelCall[],
  toolCalls: NormalizedToolCall[],
): void {
  const modelById = new Map(modelCalls.map(m => [m.id, m]));
  for (const tc of toolCalls) {
    if (!tc.agent && tc.agentCallId) {
      const mc = modelById.get(tc.agentCallId);
      if (mc?.agent) tc.agent = mc.agent;
    }
  }
}

// ─── Span utilities ───────────────────────────────────────────────────────────

export interface RawSpan {
  span_id?: string;
  parent_span_id?: string;
  name?: string;
  duration_ms?: number;
  started_at?: string;
  finished_at?: string;
  kind?: string;
  status?: string | { code?: string; message?: string };
  attributes?: Record<string, unknown>;
}

/**
 * Returns a map span_id → agentName for every span in the tree.
 * Prefers nova.agent.id attribute; falls back to the name of the nearest
 * non-root ancestor.
 */
export function buildSpanAgentMap(spans: RawSpan[]): Map<string, string> {
  const root = spans.find(s => !s.parent_span_id);
  if (!root?.span_id) return new Map();
  const rootId = root.span_id;

  const parentOf = new Map<string, string>();
  const nameOf = new Map<string, string>();
  const agentAttr = new Map<string, string>();

  for (const s of spans) {
    if (!s.span_id) continue;
    if (s.parent_span_id) parentOf.set(s.span_id, s.parent_span_id);
    nameOf.set(s.span_id, s.name ?? s.span_id.slice(0, 8));
    const aid = s.attributes?.['nova.agent.id'];
    if (aid) agentAttr.set(s.span_id, String(aid));
  }

  const agentOf = new Map<string, string>();

  function findAgent(id: string): string {
    if (agentOf.has(id)) return agentOf.get(id)!;
    if (agentAttr.has(id)) {
      const a = agentAttr.get(id)!;
      agentOf.set(id, a);
      return a;
    }
    const parent = parentOf.get(id);
    if (!parent || parent === rootId) {
      const name = nameOf.get(id) ?? id.slice(0, 8);
      agentOf.set(id, name);
      return name;
    }
    const a = findAgent(parent);
    agentOf.set(id, a);
    return a;
  }

  for (const s of spans) {
    if (s.span_id && s.span_id !== rootId) findAgent(s.span_id);
  }

  return agentOf;
}

export function spanStatusOk(status: RawSpan['status']): boolean {
  if (!status) return true;
  if (typeof status === 'string') return status === 'ok' || status === 'success';
  return !status.code || status.code === 'ok' || status.code === 'OK';
}
