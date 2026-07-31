/**
 * Shared types + small pure helpers for the Runs tab split.
 * Extracted verbatim from the former RunsTab monolith — behavior frozen.
 */

export type DetailView = 'inspect' | 'trace' | 'replay' | 'secrets' | 'children' | 'forensics';

export interface RunCostEntry {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  calls: number;
}

export function extractScenario(command: string[]): string | null {
  const idx = command.indexOf('--scenario');
  if (idx < 0 || idx + 1 >= command.length) return null;
  const parts = command[idx + 1].split('/').filter(Boolean);
  // prefer the directory segment before the filename (e.g. "c1_model_contract_break" from
  // "scenarios/c1_model_contract_break/scenario.yaml"), fall back to the filename itself.
  const candidate = parts.length >= 2 ? parts[parts.length - 2] : (parts.pop() ?? '');
  return candidate.replace(/\.yaml$/, '') || null;
}

export interface ReplayResult {
  replay_id: string;
  replay_of_run_id: string;
  mode: string;
  status: string;
  start_time: string;
  end_time: string;
  duration_ms: number;
  policy_flags_used: string[];
  env_warnings: Array<Record<string, string>>;
  model_calls_mocked: number;
  tool_calls_mocked: number;
  exit_code?: number | null;
  error?: Record<string, unknown> | null;
  dry_run_report?: string;
  // semantic-mode fields
  similarity_score?: number | null;
  matched_run_id?: string | null;
  // exact-mode fields
  exact_eligible?: boolean | null;
  exact_hash_count?: number | null;
  exact_reasons?: string[] | null;
}

export type RunAction = 'export' | 'replay' | 'dry-run' | 'redact' | 'semantic' | 'exact' | 'delete';

export interface ValidationState {
  runId: string;
  loading: boolean;
  result: { valid: boolean; errors: string[] } | null;
  error: string | null;
}

export type StatusFilter = 'all' | 'running' | 'success' | 'failure' | 'error';
export type RunSort = 'newest' | 'oldest' | 'longest' | 'shortest';
