/**
 * Build-time fixture loader. Vite resolves these imports at build, so the
 * showcase data is bundled into the static output. No runtime fetches.
 */

import registryJson from '../data/fixtures/registry.json';
import diffJson from '../data/fixtures/diff-A-vs-B.json';
import dssoStatement from '../data/fixtures/evidence-bundle/dsse-statement.json';
import predicate from '../data/fixtures/evidence-bundle/predicate.json';
import manifest from '../data/fixtures/evidence-bundle/manifest.json';
// Demo public key (Ed25519) — from demo-key.json in the same fixture directory.
// Inlined because *.pem is gitignored; this is showcase-only data, never production.
const publicKeyPem =
  '-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAXs2s7xkx/5Gpv+2l5GpozUCXbUAQU3NeLH2OgAInmWQ=\n-----END PUBLIC KEY-----\n';
import lineageJsonl from '../data/fixtures/lineage.jsonl?raw';

import runACapsule from '../data/fixtures/capsules/RUN_A/capsule.json';
import runBCapsule from '../data/fixtures/capsules/RUN_B/capsule.json';
import runCCapsule from '../data/fixtures/capsules/RUN_C/capsule.json';
import runACapsuleYaml from '../data/fixtures/capsules/RUN_A/capsule.yaml?raw';
import runAModelCalls from '../data/fixtures/capsules/RUN_A/model-calls.jsonl?raw';
import runAToolCalls from '../data/fixtures/capsules/RUN_A/tool-calls.jsonl?raw';
import runATrace from '../data/fixtures/capsules/RUN_A/trace.jsonl?raw';

import runCapsuleSchema from '../data/schemas/run-capsule.schema.json';
import diffReportSchema from '../data/schemas/diff-report.schema.json';

export interface AssetRecord {
  name: string;
  version: string;
  asset_type: 'model' | 'prompt' | 'tool' | 'dataset' | 'agent' | 'evaluation' | 'deployment';
  status: 'development' | 'promoted';
  description: string;
  spec: Record<string, unknown>;
}

export interface EvalResult {
  asset: string;
  suite: string;
  passed: boolean;
  score: number | null;
}

export interface RegistryFixture {
  schema_version: string;
  assets: AssetRecord[];
  eval_results: EvalResult[];
}

export interface LineageEdgeRecord {
  edge_id: string;
  edge_type: 'consumed' | 'produced_by' | 'replayed_from' | 'evaluated_by' | 'derived_from' | 'attested_by' | 'contains' | 'delegated_to' | 'spawned';
  source: string;
  target: string;
  capsule_run_id: string;
  confidence: string;
  created_at: string;
  direction: string;
}

export interface DiffReport {
  schema_version: string;
  run_a_id: string;
  run_b_id: string;
  summary: { changed: number; added: number; removed: number };
  sections: {
    environment: { changes: Array<{ field: string; before: unknown; after: unknown; severity: string }> };
    model_calls: {
      aligned: number;
      changed: number;
      added: number;
      removed: number;
      pairs: Array<{ index: number; a_call_id: string; b_call_id: string; changes: Array<{ field: string; before: string; after: string; severity: string }> }>;
    };
    tool_calls: { aligned: number; changed?: number; added?: number; removed?: number; pairs: Array<{ tool_name?: string; tool_call_id_a?: string; tool_call_id_b?: string; changed?: boolean; added?: boolean; removed?: boolean; result_changed?: boolean; mutation_class?: string }> };
    outputs: { changes: Array<{ path: string; before_hash: string | null; after_hash: string | null; severity?: string }> };
  };
}

export const registry = registryJson as RegistryFixture;

export const lineageEdges: LineageEdgeRecord[] = lineageJsonl
  .split('\n')
  .filter((l) => l.trim())
  .map((l) => JSON.parse(l) as LineageEdgeRecord);

export const diffReport = diffJson as DiffReport;

export const evidenceBundle = {
  dsse: dssoStatement,
  predicate,
  manifest,
  publicKeyPem,
};

export const capsules = {
  RUN_A: {
    capsule: runACapsule,
    capsuleYaml: runACapsuleYaml,
    modelCalls: runAModelCalls.split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l)),
    toolCalls: runAToolCalls.split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l)),
    trace: runATrace.split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l)),
  },
  RUN_B: { capsule: runBCapsule },
  RUN_C: { capsule: runCCapsule },
};

export const schemas = {
  runCapsule: runCapsuleSchema,
  diffReport: diffReportSchema,
};
