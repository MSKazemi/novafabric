// Shared types + input styling for the KG tab panels. Extracted verbatim from
// KGTab.tsx (dashboard-modernization split).
//
// DB-KG-1 — Capsule Knowledge Graph tab (v0.18.0)
// Backend: GET /api/kg/status, GET /api/kg/agents/{id}/edges
// Reference: design/adr/0067-capsule-knowledge-graph-v1.md

export interface KGStatus {
  store: string;
  store_health: string;
  db_path: string;
  edge_count: number;
  node_counts?: Record<string, number>;
  note?: string;
}

export interface KGTopology {
  ok: boolean;
  node_counts: Record<string, number>;
  edge_counts: Record<string, number>;
  nodes: Array<{ id: string; type: string; name?: string; provider?: string; url?: string }>;
  edges: Array<{ src: string; src_type: string; dst: string; dst_type: string; edge_type: string; call_count: number; confidence: number }>;
  truncated?: boolean;
  truncated_reason?: string[];
  note?: string;
  error?: string;
}

export interface KGEdge {
  call_count: number;
  verified_count: number;
  confidence: number;
}

export interface ModelEdge extends KGEdge { model_id: string }
export interface ToolEdge extends KGEdge { tool_id: string }
export interface MCPServerEdge { server_id: string; server_name: string; call_count: number }

export const inputClass =
  'text-xs rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-2 py-1.5 font-mono focus:border-[var(--color-accent)] focus:outline-none';

export const labelClass =
  'text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]';
