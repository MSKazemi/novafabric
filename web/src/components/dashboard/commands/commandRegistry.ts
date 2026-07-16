import { GENERATED_COMMANDS } from './generatedCommands';

export type FieldType = 'text' | 'number' | 'select' | 'toggle';
export type Journey = 'debug' | 'govern' | 'audit' | 'infra';

// `flag` overrides the emitted option string. When absent, the builder falls
// back to `--${key}` (the historic behaviour used by the hand-curated defs).
// Generated defs always set `flag` so the exact CLI option name is preserved
// even when it differs from the field key (e.g. key `output-dir` → `--output-dir`).
export type CommandField =
  | { key: string; label: string; type: 'text' | 'number' | 'toggle'; required?: boolean; positional?: boolean; defaultValue?: string; suggestions?: string[]; hint: string; runnerOption?: boolean; flag?: string; visibleWhen?: { field: string; value: string }; sectionBefore?: string }
  | { key: string; label: string; type: 'select'; required?: boolean; positional?: boolean; defaultValue?: string; options: string[]; hint: string; runnerOption?: boolean; flag?: string; visibleWhen?: { field: string; value: string }; sectionBefore?: string };

export interface CommandDef {
  id: string;
  name: string;
  journey: Journey;
  description: string;
  fields: CommandField[];
  docsPath: string;
  nativeTabNote?: string;
  /** True for defs derived automatically from the Typer app (vs. hand-curated). */
  generated?: boolean;
}

export function buildCommandString(
  cmd: CommandDef,
  values: Record<string, string>,
): string {
  const parts: string[] = [cmd.name];

  for (const field of cmd.fields) {
    // Skip fields whose visibility condition is not met
    if (field.visibleWhen && values[field.visibleWhen.field] !== field.visibleWhen.value) {
      continue;
    }

    const val = values[field.key] ?? field.defaultValue ?? '';
    if (!val) continue;

    const flag = field.flag ?? `--${field.key}`;
    if (field.type === 'toggle') {
      if (val === 'true') parts.push(flag);
    } else if (field.positional) {
      parts.push(val);
    } else if (field.runnerOption) {
      parts.push('--runner-option', `${field.key}=${val}`);
    } else {
      parts.push(flag, val);
    }
  }

  return parts.join(' ');
}

const CURATED_COMMANDS: readonly CommandDef[] = [
  // ── Debug & Investigate ────────────────────────────────────────────────
  {
    id: 'nova-capture',
    name: 'nova capture',
    journey: 'debug',
    description: 'Run a command under NovaFabric capture — records the full execution as a Run Capsule including model calls, tool calls, trace events, environment snapshot, and exit code.',
    fields: [
      {
        key: 'command', label: 'Command', type: 'text', required: true, positional: true,
        suggestions: [
          'python your_agent.py',
          'uv run python your_agent.py',
          'python -m your_module',
          'node agent.js',
          'bash run_agent.sh',
        ],
        hint: 'The command to run. NovaFabric wraps it and captures all activity.',
      },
      { key: 'scenario', label: '--scenario', type: 'text', hint: 'Scenario label attached to the capsule for filtering in the Runs tab.' },
      { key: 'runner', label: '--runner', type: 'select', options: ['local', 'docker', 'kubernetes', 'slurm', 'pbs', 'lsf'], defaultValue: 'local', hint: 'Execution environment. Docker/Kubernetes/Slurm/PBS/LSF require additional cluster config.' },
      { key: 'timeout', label: '--timeout', type: 'number', hint: 'Seconds before capture aborts. Default: no timeout.' },
      { key: 'image', label: '--runner-option image', type: 'text', runnerOption: true,
        visibleWhen: { field: 'runner', value: 'docker' },
        sectionBefore: 'Docker runner options',
        hint: 'Required for Docker runner. Fully-qualified image reference with novafabric pip-installed (e.g. myorg/agent-runtime:abc1234). The container must have novafabric installed — see Capture tab for a starter Dockerfile.' },
      { key: 'network', label: '--runner-option network', type: 'text', runnerOption: true,
        visibleWhen: { field: 'runner', value: 'docker' },
        hint: 'Docker network to attach (e.g. bridge, host, my-net). Defaults to Docker bridge. host is allowed but discouraged.' },
      { key: 'workdir', label: '--runner-option workdir', type: 'text', runnerOption: true,
        visibleWhen: { field: 'runner', value: 'docker' },
        hint: "Override the container working directory. Defaults to the image's WORKDIR." },
      { key: 'user', label: '--runner-option user', type: 'text', runnerOption: true,
        visibleWhen: { field: 'runner', value: 'docker' },
        hint: 'Run as this user:group (e.g. 1000:1000). Recommended to prevent root-owned capsule files in the mounted volume.' },
      { key: 'asset', label: '--asset', type: 'text',
        sectionBefore: 'Asset status enforcement (optional)',
        hint: 'Named asset to check before capture starts (e.g. my-agent@v1). Use with --require-asset-status or --warn-if-asset-status.' },
      { key: 'require-asset-status', label: '--require-asset-status', type: 'text',
        hint: 'Comma-separated statuses the asset must be in to allow capture (e.g. staging,production). Blocks if not satisfied.' },
      { key: 'warn-if-asset-status', label: '--warn-if-asset-status', type: 'text',
        hint: 'Comma-separated statuses that emit a warning but do not block capture (e.g. development).' },
      { key: 'capture-env', label: '--capture-env', type: 'toggle', hint: 'Snapshot environment variables into the capsule.' },
      { key: 'no-redact', label: '--no-redact', type: 'toggle', hint: 'Skip automatic secret redaction. Use with caution.' },
      { key: 'dry-run', label: '--dry-run', type: 'toggle', hint: 'Validate the command without executing it.' },
    ],
    docsPath: '/spec#capture',
  },
  {
    id: 'nova-replay',
    name: 'nova replay',
    journey: 'debug',
    description: 'Replay a Run Capsule in forensic or mocked mode. Forensic is read-only; mocked re-executes agent code using cached model/tool responses.',
    fields: [
      { key: 'run-id', label: 'run-id', type: 'text', required: true, positional: true, hint: 'The run_id or capsule path to replay.' },
      { key: 'mode', label: '--mode', type: 'select', options: ['forensic', 'mocked', 'semantic', 'exact'], defaultValue: 'forensic', hint: 'forensic: read-only inspection. mocked: re-execute with cached responses. semantic/exact: match by semantics or exact hash.' },
      { key: 'output', label: '--output', type: 'text', hint: 'Directory to write the replay capsule. Defaults to a timestamped sub-directory.' },
    ],
    docsPath: '/spec#replay',
    nativeTabNote: 'Forensic and dry-run replay are also available from the Runs tab → "Replay" button.',
  },
  {
    id: 'nova-diff',
    name: 'nova diff',
    journey: 'debug',
    description: 'Compare two Run Capsules structurally — environment, model calls, tool calls, and outputs. Emits a diff report.',
    fields: [
      { key: 'run-a', label: 'run-a', type: 'text', required: true, positional: true, hint: 'First run_id or capsule path (baseline).' },
      { key: 'run-b', label: 'run-b', type: 'text', required: true, positional: true, hint: 'Second run_id or capsule path (candidate).' },
      { key: 'format', label: '--format', type: 'select', options: ['json', 'markdown'], defaultValue: 'json', hint: 'Output format. Use markdown for human-readable reports.' },
    ],
    docsPath: '/spec#diff',
    nativeTabNote: 'You can also diff runs visually in the Diff tab → or use "Compare" from the Runs tab.',
  },
  {
    id: 'nova-verify',
    name: 'nova verify',
    journey: 'debug',
    description: 'Verify a Run Capsule\'s NovaSeal signature — checks DSSE envelope (ECDSA P-256), RFC 3161 timestamp, and Merkle log inclusion. Requires NovaSeal config (ADR-0041).',
    fields: [
      { key: 'run-id', label: 'run-id', type: 'text', required: true, positional: true, hint: 'The run_id or capsule path to verify.' },
      { key: 'verbose', label: '--verbose', type: 'toggle', hint: 'Show full verification trace including signature bytes and log proof.' },
    ],
    docsPath: '/spec#verify',
    nativeTabNote: 'Evidence bundles can also be verified from the Evidence tab.',
  },
  {
    id: 'nova-run-show',
    name: 'nova run show',
    journey: 'debug',
    description: 'Show a capsule with its parent/child hierarchy — lists parent, sibling workers, and child capsules in a distributed run (Phase 3, ADR-0044).',
    fields: [
      { key: 'run-id', label: 'run-id', type: 'text', required: true, positional: true, hint: 'The run_id of the capsule to show.' },
      { key: 'with-children', label: '--with-children', type: 'toggle', hint: 'Include child worker capsules in the output (for PARENT capsules in distributed runs).' },
      { key: 'format', label: '--format', type: 'select', options: ['text', 'json'], defaultValue: 'text', hint: 'Output format.' },
    ],
    docsPath: '/spec#run-show',
    nativeTabNote: 'The parent/child capsule tree is also visible in the Runs tab → run detail → "children" view.',
  },
  {
    id: 'nova-run-validate-distributed',
    name: 'nova run validate-distributed',
    journey: 'debug',
    description: 'Validate a distributed (parent/child) capsule set — checks completeness, schema conformance, typed edge vocabulary, and PARTIALLY_COMPLETE state rules.',
    fields: [
      { key: 'run-id', label: 'parent-run-id', type: 'text', required: true, positional: true, hint: 'The run_id of the PARENT capsule.' },
      { key: 'strict', label: '--strict', type: 'toggle', hint: 'Fail on any PARTIALLY_COMPLETE child (default: warn).' },
    ],
    docsPath: '/spec#run-validate-distributed',
  },
  {
    id: 'nova-report',
    name: 'nova report',
    journey: 'debug',
    description: 'Generate a human-readable or machine-readable report for a Run Capsule — model calls, tool calls, eval results, and timing.',
    fields: [
      { key: 'run-id', label: 'run-id', type: 'text', required: true, positional: true, hint: 'The run_id or capsule path to report on.' },
      { key: 'format', label: '--format', type: 'select', options: ['markdown', 'json'], defaultValue: 'markdown', hint: 'Output format.' },
      { key: 'output', label: '--output', type: 'text', hint: 'File path to write the report. Defaults to stdout.' },
    ],
    docsPath: '/spec#report',
  },

  // ── Govern & Promote ──────────────────────────────────────────────────
  {
    id: 'nova-register',
    name: 'nova register',
    journey: 'govern',
    description: 'Register a new asset version from a YAML spec file. The spec is validated against asset-spec.schema.json before writing.',
    fields: [
      { key: 'spec-file', label: 'spec-file', type: 'text', required: true, positional: true, hint: 'Path to the asset spec YAML (e.g. ./specs/summarizer-v2.yaml).' },
    ],
    docsPath: '/spec#register',
    nativeTabNote: 'You can also register assets from the Registry tab → "+ Register asset"',
  },
  {
    id: 'nova-validate',
    name: 'nova validate',
    journey: 'govern',
    description: 'Validate an asset spec YAML against the schema without writing it to the registry. Use to check a spec before registering.',
    fields: [
      { key: 'spec-file', label: 'spec-file', type: 'text', required: true, positional: true, hint: 'Path to the asset spec YAML to validate.' },
    ],
    docsPath: '/spec#validate',
  },
  {
    id: 'nova-suggest-register',
    name: 'nova suggest-register',
    journey: 'govern',
    description: 'Auto-suggest asset registration specs from captured Run Capsules — inspects model/tool metadata and emits ready-to-use YAML stubs.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', positional: true, hint: 'Capsule directory to inspect. Omit to scan all recent runs.' },
      { key: 'output', label: '--output', type: 'text', hint: 'Directory to write generated spec stubs. Defaults to stdout.' },
    ],
    docsPath: '/spec#suggest-register',
    nativeTabNote: 'Suggestions are also shown in the Registry tab → "Suggestions" banner.',
  },
  {
    id: 'nova-eval',
    name: 'nova eval run',
    journey: 'govern',
    description: 'Run one or all eval suites against a registered asset version. Results are stored and used to gate promotion.',
    fields: [
      { key: 'asset', label: 'asset@version', type: 'text', required: true, positional: true, hint: 'Asset name and version (e.g. summarizer-v2@0.4.1).' },
      { key: 'suite', label: '--suite', type: 'text', hint: 'Run a specific suite by name. Omit to run all suites for this asset.' },
      {
        key: 'suite-select', label: '--suite (standard)', type: 'select',
        options: ['', 'novafabric-smoke-v1', 'gaia-l1', 'swe-bench-lite', 'agentbench-os', 'mmlu-v1', 'truthfulqa-v1'],
        defaultValue: '',
        hint: 'Quick-pick a standard eval suite (OCI-pinned containers, v0.9).',
      },
      { key: 'output', label: '--output', type: 'text', hint: 'Path to write the EvalResult JSON. Defaults to the capsule directory.' },
    ],
    docsPath: '/spec#eval',
    nativeTabNote: 'Eval suites can also be run from the Registry tab → asset detail → "Run eval", or from the Governance tab.',
  },
  {
    id: 'nova-promote',
    name: 'nova promote',
    journey: 'govern',
    description: 'Promote an asset to a new lifecycle status (staging → production). Eval-gated: Rego policy must pass before promotion is recorded.',
    fields: [
      { key: 'asset', label: 'asset@version', type: 'text', required: true, positional: true, hint: 'Asset name and version to promote (e.g. summarizer-v2@0.4.1).' },
      { key: 'to', label: '--to', type: 'select', options: ['staging', 'production', 'deprecated'], defaultValue: 'production', hint: 'Target lifecycle stage.' },
      { key: 'note', label: '--note', type: 'text', hint: 'Human-readable promotion rationale logged in the audit trail.' },
    ],
    docsPath: '/spec#promote',
    nativeTabNote: 'Promotion is also available from the Registry tab → asset detail → "Promote".',
  },
  {
    id: 'nova-approve',
    name: 'nova approve',
    journey: 'govern',
    description: 'Record a maker-checker sign-off approval for an asset version — required for promotion when the policy demands ≥N approvals.',
    fields: [
      { key: 'asset', label: 'asset@version', type: 'text', required: true, positional: true, hint: 'Asset name and version to approve (e.g. summarizer-v2@0.4.1).' },
      { key: 'role', label: '--role', type: 'select', options: ['reviewer', 'security', 'cto', 'compliance'], defaultValue: 'reviewer', hint: 'Approver role. Must match the policy\'s required roles list.' },
      { key: 'note', label: '--note', type: 'text', hint: 'Sign-off comment recorded in the evidence bundle.' },
    ],
    docsPath: '/spec#approve',
  },
  {
    id: 'nova-rollback',
    name: 'nova rollback',
    journey: 'govern',
    description: 'Atomically rollback an asset to its previous production version — archives the current production version and re-promotes the prior one.',
    fields: [
      { key: 'asset', label: 'asset-name', type: 'text', required: true, positional: true, hint: 'Asset name to rollback (e.g. summarizer-v2). Rolls back to the last production version.' },
      { key: 'to-version', label: '--to-version', type: 'text', hint: 'Explicit version to restore. Omit to use the last known-good production version.' },
      { key: 'note', label: '--note', type: 'text', hint: 'Rollback reason recorded in the audit log.' },
    ],
    docsPath: '/spec#rollback',
  },
  {
    id: 'nova-unregister',
    name: 'nova unregister',
    journey: 'govern',
    description: 'Hard-delete an asset version from the registry. Blocked if the asset is in staging, production, or pending_approval unless --force is provided. Deletion is permanent and logged in the audit trail.',
    fields: [
      { key: 'asset', label: 'asset@version', type: 'text', required: true, positional: true, hint: 'Asset name and version to unregister (e.g. summarizer-v2@0.4.1).' },
      { key: 'force', label: '--force', type: 'toggle', hint: 'Override status guard — allows deletion of staging/production/pending_approval assets. Use with caution.' },
      { key: 'note', label: '--note', type: 'text', hint: 'Reason for deletion, recorded in the audit trail.' },
    ],
    docsPath: '/spec#unregister',
    nativeTabNote: 'Unregister is also available from the Registry tab → asset detail → "Unregister" button.',
  },
  {
    id: 'nova-promote-propose',
    name: 'nova promote propose',
    journey: 'govern',
    description: 'Submit a promotion proposal for maker-checker review (ADR-0059). The proposal is DSSE-signed and recorded; a second approver must run "nova promote approve" before the promotion takes effect.',
    fields: [
      { key: 'asset', label: 'asset@version', type: 'text', required: true, positional: true, hint: 'Asset name and version to propose for promotion (e.g. summarizer-v2@0.4.1).' },
      { key: 'to', label: '--to', type: 'select', options: ['staging', 'production', 'deprecated'], defaultValue: 'production', hint: 'Target lifecycle stage.' },
      { key: 'note', label: '--note', type: 'text', hint: 'Proposal rationale recorded in the linked envelope.' },
    ],
    docsPath: '/spec#promote-propose',
  },
  {
    id: 'nova-promote-approve',
    name: 'nova promote approve',
    journey: 'govern',
    description: 'Approve a pending promotion proposal (ADR-0059). The approver must be a different identity than the proposer (SoD enforced at crypto+identity level). Completes the maker-checker chain.',
    fields: [
      { key: 'asset', label: 'asset@version', type: 'text', required: true, positional: true, hint: 'Asset name and version to approve (e.g. summarizer-v2@0.4.1).' },
      { key: 'role', label: '--role', type: 'select', options: ['reviewer', 'security', 'cto', 'compliance'], defaultValue: 'reviewer', hint: 'Approver role. Must match the policy\'s required roles list.' },
      { key: 'note', label: '--note', type: 'text', hint: 'Approval comment recorded in the linked envelope.' },
    ],
    docsPath: '/spec#promote-approve',
  },
  {
    id: 'nova-eval-compare',
    name: 'nova eval compare',
    journey: 'govern',
    description: 'Compare two EvalResult JSON files — shows score deltas, regression flags, and suite-level summaries side by side.',
    fields: [
      { key: 'result-a', label: 'result-a.json', type: 'text', required: true, positional: true, hint: 'Path to the baseline EvalResult JSON (e.g. eval_results/summarizer-v1.json).' },
      { key: 'result-b', label: 'result-b.json', type: 'text', required: true, positional: true, hint: 'Path to the candidate EvalResult JSON (e.g. eval_results/summarizer-v2.json).' },
      { key: 'format', label: '--format', type: 'select', options: ['text', 'json', 'markdown'], defaultValue: 'text', hint: 'Output format.' },
    ],
    docsPath: '/spec#eval-compare',
    nativeTabNote: 'Eval trends and cross-run comparisons are also visible in the Registry tab → asset detail → "Eval trend".',
  },
  {
    id: 'nova-eval-list',
    name: 'nova eval list',
    journey: 'govern',
    description: 'List all registered eval suite adapters — shows suite ID, version, OCI digest, and entry point for every adapter in the novafabric.eval_suites group.',
    fields: [],
    docsPath: '/spec#eval',
    nativeTabNote: 'Registered eval suites are also listed in the Governance tab.',
  },
  {
    id: 'nova-eval-agent',
    name: 'nova eval agent',
    journey: 'govern',
    description: 'Run an eval suite against an asset using the legacy v0.1 single-agent interface. Prefer "nova eval run" for new work; this interface is retained for backward compatibility.',
    fields: [
      { key: 'asset', label: 'asset@version', type: 'text', required: true, positional: true, hint: 'Asset name and version (e.g. summarizer-v2@0.4.1).' },
      { key: 'suite', label: '--suite', type: 'text', hint: 'Eval suite name to run (e.g. novafabric-smoke-v1).' },
    ],
    docsPath: '/spec#eval',
  },

  // ── Govern: Risk Classification (v0.16 — ADR-0056) ────────────────────
  {
    id: 'nova-classify-run',
    name: 'nova classify run',
    journey: 'govern',
    description: 'Classify an AI system risk tier against EU AI Act, NIST AI RMF, or OMB M-24-10 using a YAML system description.',
    fields: [
      { key: 'system', label: '--system', type: 'text', required: true, hint: 'Path to the AI system YAML description file.' },
      { key: 'vocabulary', label: '--vocabulary', type: 'select', options: ['eu-ai-act/2024.1.0', 'nist-ai-rmf/1.0.0', 'omb-m-24-10/1.0.0'], defaultValue: 'eu-ai-act/2024.1.0', hint: 'Regulatory vocabulary to classify against.' },
      { key: 'format', label: '--format', type: 'select', options: ['text', 'json'], defaultValue: 'text', hint: 'Output format.' },
    ],
    docsPath: '/spec#classify',
    nativeTabNote: 'Risk classification is also available from the Governance tab.',
  },
  {
    id: 'nova-classify-from-capsule',
    name: 'nova classify from-capsule',
    journey: 'govern',
    description: 'Infer AI system record from a captured Run Capsule and classify its risk tier.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory to classify from.' },
      { key: 'vocabulary', label: '--vocabulary', type: 'select', options: ['eu-ai-act/2024.1.0', 'nist-ai-rmf/1.0.0', 'omb-m-24-10/1.0.0'], defaultValue: 'eu-ai-act/2024.1.0', hint: 'Regulatory vocabulary to classify against.' },
      { key: 'format', label: '--format', type: 'select', options: ['text', 'json'], defaultValue: 'text', hint: 'Output format.' },
    ],
    docsPath: '/spec#classify',
    nativeTabNote: 'Classification from capsule is also available from the Governance tab.',
  },
  {
    id: 'nova-classify-list-vocabularies',
    name: 'nova classify list-vocabularies',
    journey: 'govern',
    description: 'List all available classification vocabularies (EU AI Act, NIST AI RMF, OMB M-24-10).',
    fields: [],
    docsPath: '/spec#classify',
    nativeTabNote: 'Vocabularies are also listed in the Governance tab.',
  },

  // ── Audit & Verify ────────────────────────────────────────────────────
  {
    id: 'nova-scan-secrets',
    name: 'nova scan-secrets',
    journey: 'audit',
    description: 'Scan a Run Capsule for secrets (API keys, tokens, credentials) using SecretScannerV0. Prints findings without modifying the capsule.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory (e.g. .novafabric/runs/<run_id>/).' },
      { key: 'fail-on', label: '--fail-on', type: 'select', options: ['critical', 'high', 'medium', 'low', 'info'], hint: 'Exit 1 if any finding at or above this severity is found. Useful for blocking CI pipelines.' },
    ],
    docsPath: '/spec#scan-secrets',
    nativeTabNote: 'Secret scan findings are also visible in the Runs tab → run detail → "secrets" view.',
  },
  {
    id: 'nova-redact',
    name: 'nova redact',
    journey: 'audit',
    description: 'Re-scan a capsule for secrets and rewrite its redaction-proof.json — use after updating redaction patterns or when a new secret type is discovered.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory to re-redact.' },
      { key: 'force', label: '--force', type: 'toggle', hint: 'Overwrite existing redaction-proof.json without prompting.' },
    ],
    docsPath: '/spec#redact',
    nativeTabNote: 'Redaction is also available from the Runs tab → run detail → "Redact" button.',
  },
  {
    id: 'nova-export-evidence',
    name: 'nova export-evidence',
    journey: 'audit',
    description: 'Build a tamper-evident, ed25519-signed Evidence Bundle ZIP from a Run Capsule. The bundle includes a DSSE statement and can be verified offline.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory.' },
      { key: 'output', label: '--output', type: 'text', hint: 'Output path for the ZIP. Defaults to ~/.novafabric/evidence/<run_id>.zip.' },
      { key: 'key', label: '--key', type: 'text', hint: 'Path to signing key PEM. Auto-generated at ~/.novafabric/keys/local-key.pem if omitted.' },
    ],
    docsPath: '/spec#export-evidence',
    nativeTabNote: 'Evidence bundles can also be exported from the Runs tab → run detail → "Export evidence".',
  },
  {
    id: 'nova-lineage-provenance',
    name: 'nova lineage provenance',
    journey: 'audit',
    description: 'Walk the provenance chain (ancestors) of a node — shows every upstream asset, capsule, or run that contributed to producing the given ref.',
    fields: [
      { key: 'ref', label: 'ref', type: 'text', required: true, positional: true, hint: 'Node reference: asset name@version, run_id, or any lineage node ID.' },
      { key: 'depth', label: '--depth', type: 'number', defaultValue: '5', hint: 'Maximum hops to traverse upstream. Default: 5.' },
      { key: 'format', label: '--format', type: 'select', options: ['text', 'json', 'openlineage'], defaultValue: 'text', hint: 'Output format. openlineage emits OLAF-compatible JSON.' },
    ],
    docsPath: '/spec#lineage',
    nativeTabNote: 'Provenance queries are also available in the Lineage tab → "Query Provenance" panel.',
  },
  {
    id: 'nova-lineage-blast-radius',
    name: 'nova lineage blast-radius',
    journey: 'audit',
    description: 'Walk the blast radius (descendants) of a node — shows every downstream asset or run that depends on the given ref. Essential for impact analysis.',
    fields: [
      { key: 'ref', label: 'ref', type: 'text', required: true, positional: true, hint: 'Node reference to fan out from (e.g. summarizer-v2@0.4.1).' },
      { key: 'depth', label: '--depth', type: 'number', defaultValue: '5', hint: 'Maximum hops to traverse downstream. Default: 5.' },
      { key: 'format', label: '--format', type: 'select', options: ['text', 'json'], defaultValue: 'text', hint: 'Output format.' },
    ],
    docsPath: '/spec#lineage',
    nativeTabNote: 'Blast-radius queries are also available in the Lineage tab → "Query Blast Radius" panel.',
  },
  {
    id: 'nova-lineage-replay-chain',
    name: 'nova lineage replay-chain',
    journey: 'audit',
    description: 'Query the replay chain for a run — shows the sequence of replays derived from an original capsule, including mode and outcome of each replay.',
    fields: [
      { key: 'run-id', label: 'run-id', type: 'text', required: true, positional: true, hint: 'The original run_id whose replay chain you want to trace.' },
    ],
    docsPath: '/spec#lineage',
    nativeTabNote: 'Replay chains are also queryable in the Lineage tab → "Query Replay Chain" panel.',
  },
  {
    id: 'nova-lineage-time-travel',
    name: 'nova lineage time-travel',
    journey: 'audit',
    description: 'Query lineage state as it existed at a past timestamp — reconstructs the provenance graph at any historical point (ADR-0047).',
    fields: [
      { key: 'ref', label: 'ref', type: 'text', required: true, positional: true, hint: 'Node reference to query provenance for.' },
      { key: 'at', label: '--at', type: 'text', required: true, hint: 'ISO-8601 timestamp (e.g. 2026-03-01T12:00:00Z). Queries the graph as of this moment.' },
      { key: 'depth', label: '--depth', type: 'number', defaultValue: '5', hint: 'Maximum hops to traverse.' },
    ],
    docsPath: '/spec#lineage',
  },
  {
    id: 'nova-lineage-emit-openlineage',
    name: 'nova lineage emit-openlineage',
    journey: 'audit',
    description: 'Emit OpenLineage events for the lineage graph — publishes OLAF-compatible RunEvent/DatasetEvent JSON to stdout or a target HTTP endpoint.',
    fields: [
      { key: 'run-id', label: 'run-id', type: 'text', positional: true, hint: 'Emit events for this specific run_id. Omit to emit all stored edges.' },
      { key: 'transport', label: '--transport', type: 'select', options: ['stdout', 'http'], defaultValue: 'stdout', hint: 'stdout: print events. http: POST to --url endpoint.' },
      { key: 'url', label: '--url', type: 'text', hint: 'HTTP endpoint URL (required when --transport http, e.g. https://marquez.internal/api/v1/lineage).' },
    ],
    docsPath: '/spec#lineage',
  },
  {
    id: 'nova-hold-create',
    name: 'nova hold create',
    journey: 'audit',
    description: 'Place a legal hold on a registry — prevents capsule deletion, promotion, and rollback until released. Logged in the audit trail (ADR-0031).',
    fields: [
      { key: 'registry', label: 'registry', type: 'text', required: true, positional: true, hint: 'Registry identifier to hold (e.g. production, staging, or a specific asset name).' },
      { key: 'reason', label: '--reason', type: 'text', required: true, hint: 'Human-readable hold reason (e.g. "Litigation hold — case #2026-047").' },
      { key: 'expires-in', label: '--expires-in', type: 'text', hint: 'Optional duration string (e.g. 90d, 6m). Omit for indefinite hold.' },
    ],
    docsPath: '/spec#holds',
    nativeTabNote: 'Holds can also be created and released from the Holds tab.',
  },
  {
    id: 'nova-policy-test',
    name: 'nova policy test',
    journey: 'audit',
    description: 'Test an OPA/Rego policy rule interactively — submit an action, subject, and resource and check whether the policy allows or denies.',
    fields: [
      { key: 'action', label: '--action', type: 'select', options: ['promote', 'register', 'approve', 'rollback', 'export_evidence', 'replay', 'redact'], defaultValue: 'promote', hint: 'The action to check.' },
      { key: 'subject-role', label: '--subject-role', type: 'text', hint: 'Role of the requesting subject (e.g. reviewer, admin).' },
      { key: 'resource', label: '--resource', type: 'text', hint: 'Resource identifier (e.g. summarizer-v2@0.4.1).' },
    ],
    docsPath: '/spec#policy',
    nativeTabNote: 'Policy rules can also be tested interactively in the Policy tab.',
  },
  {
    id: 'nova-policy-explain',
    name: 'nova policy explain',
    journey: 'audit',
    description: 'Explain the reasoning behind an OPA/Rego policy decision — shows which rule matched, the input bindings, and the decision trace.',
    fields: [
      { key: 'action', label: '--action', type: 'select', options: ['promote', 'register', 'approve', 'rollback', 'export_evidence', 'replay', 'redact'], defaultValue: 'promote', hint: 'The action to explain.' },
      { key: 'subject-role', label: '--subject-role', type: 'text', hint: 'Role of the requesting subject.' },
      { key: 'resource', label: '--resource', type: 'text', hint: 'Resource identifier.' },
    ],
    docsPath: '/spec#policy',
  },
  {
    id: 'nova-policy-list',
    name: 'nova policy list',
    journey: 'audit',
    description: 'List Rego policy files and signed promotion policies — shows the active policy bundle contents and every signed policy version in the PolicyStore.',
    fields: [
      { key: 'namespace', label: '--namespace', type: 'text', hint: 'Filter signed policies to this namespace. Omit to list all namespaces.' },
      { key: 'bundle', label: '--bundle', type: 'text', hint: 'Rego bundle directory. Defaults to the built-in bundle.' },
    ],
    docsPath: '/spec#policy',
    nativeTabNote: 'The policy inventory is also visible in the Policy tab → "Policy Inventory" panel.',
  },
  {
    id: 'nova-policy-sign',
    name: 'nova policy sign',
    journey: 'audit',
    description: 'Sign a promotion policy bundle with a DSSE envelope (ADR-0059). The signed bundle is written to the trust store and used by "nova seal propose" to enforce maker-checker SoD.',
    fields: [
      { key: 'bundle-path', label: '--bundle-path', type: 'text', required: true, hint: 'Path to the unsigned policy bundle JSON (e.g. ./policy/promotion-v1.json).' },
      { key: 'key', label: '--key', type: 'text', required: true, hint: 'Path to the signing key PEM (Ed25519 or ECDSA P-256).' },
      { key: 'cert', label: '--cert', type: 'text', hint: 'Path to the signing certificate PEM. Required for Rekor log inclusion.' },
    ],
    docsPath: '/spec#policy-sign',
    nativeTabNote: 'Policy signing is also available from the Policy tab → "Sign Promotion Policy" panel; the active signed policy is shown in the Seal tab → "Promotion Policy" panel.',
  },
  {
    id: 'nova-lineage-import',
    name: 'nova lineage import',
    journey: 'audit',
    description: 'Import lineage edges from a Parquet file (e.g. exported from Spark/dbt) into the NovaFabric lineage store.',
    fields: [
      { key: 'from', label: '--from', type: 'text', required: true, hint: 'Path to the source Parquet file containing lineage edges.' },
      { key: 'store', label: '--store', type: 'select', options: ['sqlite', 'postgres', 'kuzudb'], defaultValue: 'sqlite', hint: 'Target lineage store backend.' },
      { key: 'dry-run', label: '--dry-run', type: 'toggle', hint: 'Validate the import without writing any edges.' },
    ],
    docsPath: '/spec#lineage-import',
  },
  {
    id: 'nova-seal-bypass',
    name: 'nova seal bypass',
    journey: 'audit',
    description: 'Create a time-limited bypass of the maker-checker SoD requirement (ADR-0059). Maximum 7 days (168 hours). The bypass is DSSE-signed and permanently recorded in the Merkle audit log. Requires justification and signing key.',
    fields: [
      { key: 'capsule-id', label: '--capsule-id', type: 'text', required: true, hint: 'The run_id of the capsule to bypass SoD for.' },
      { key: 'duration', label: '--duration', type: 'text', required: true, defaultValue: '24h', hint: 'Bypass window (e.g. 24h, 48h). Maximum: 168h (7 days).' },
      { key: 'key', label: '--key', type: 'text', required: true, hint: 'Path to the admin signing key PEM.' },
      { key: 'justification', label: '--justification', type: 'text', required: true, hint: 'Human-readable reason for the bypass. Logged permanently in the audit trail.' },
    ],
    docsPath: '/spec#seal-bypass',
  },
  {
    id: 'nova-seal-log-verify',
    name: 'nova seal log verify',
    journey: 'audit',
    description: 'Verify Merkle log consistency — checks that all log entries are cryptographically linked and none have been tampered with (ADR-0041). Pass --capsule-id to verify inclusion of a specific capsule.',
    fields: [
      { key: 'capsule-id', label: '--capsule-id', type: 'text', hint: 'Optional: verify that this capsule\'s entry is included in the log. Omit to verify the full log.' },
    ],
    docsPath: '/spec#seal-log-verify',
    nativeTabNote: 'Log integrity is verifiable from the Seal tab → "Merkle Log Verify" panel.',
  },
  // ── Compliance audit (v0.16 — ADR-0061) ───────────────────────────────
  {
    id: 'nova-audit-map',
    name: 'nova audit map',
    journey: 'audit',
    description: 'List evidence checkers for a compliance profile — shows what evidence types each control requires.',
    fields: [
      { key: 'profile', label: '--profile', type: 'select', options: ['nist-ai-rmf', 'eu-ai-act-high-risk', 'gdpr', 'soc2-type2', 'iso42001', 'scientific-reproducibility'], defaultValue: 'nist-ai-rmf', hint: 'Compliance profile to inspect.' },
    ],
    docsPath: '/spec#audit-map',
    nativeTabNote: 'Audit checkers are shown in the Compliance tab → "Compliance Audit" panel.',
  },
  {
    id: 'nova-audit-report',
    name: 'nova audit report',
    journey: 'audit',
    description: 'Run a compliance audit against a capsule — maps captured evidence to regulatory controls and produces a coverage report.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory to audit.' },
      { key: 'profile', label: '--profile', type: 'select', options: ['nist-ai-rmf', 'eu-ai-act-high-risk', 'gdpr', 'soc2-type2', 'iso42001', 'scientific-reproducibility'], defaultValue: 'nist-ai-rmf', hint: 'Compliance profile to audit against.' },
      { key: 'format', label: '--format', type: 'select', options: ['text', 'json'], defaultValue: 'text', hint: 'Output format.' },
    ],
    docsPath: '/spec#audit-report',
    nativeTabNote: 'Compliance audit is also available from the Compliance tab → "Compliance Audit" panel.',
  },
  {
    id: 'nova-audit-verify',
    name: 'nova audit verify',
    journey: 'audit',
    description: 'Assert a capsule meets a minimum coverage threshold for a compliance profile.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory.' },
      { key: 'profile', label: '--profile', type: 'select', options: ['nist-ai-rmf', 'eu-ai-act-high-risk', 'gdpr', 'soc2-type2', 'iso42001', 'scientific-reproducibility'], defaultValue: 'nist-ai-rmf', hint: 'Compliance profile to verify against.' },
      { key: 'min-coverage', label: '--min-coverage', type: 'number', defaultValue: '0.7', hint: 'Minimum coverage fraction (0.0–1.0). Default: 0.7.' },
    ],
    docsPath: '/spec#audit-verify',
  },
  {
    id: 'nova-audit-bundle',
    name: 'nova audit bundle',
    journey: 'audit',
    description: 'Export a signed audit bundle ZIP from a capsule and compliance profile — suitable for regulatory submission.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory.' },
      { key: 'profile', label: '--profile', type: 'select', options: ['nist-ai-rmf', 'eu-ai-act-high-risk', 'gdpr', 'soc2-type2', 'iso42001', 'scientific-reproducibility'], defaultValue: 'nist-ai-rmf', hint: 'Compliance profile to bundle.' },
      { key: 'output', label: '--output', type: 'text', required: true, hint: 'Output path for the audit bundle ZIP.' },
    ],
    docsPath: '/spec#audit-bundle',
  },
  {
    id: 'nova-audit-coverage',
    name: 'nova audit coverage',
    journey: 'audit',
    description: 'Print a coverage summary for all profiles against a capsule — shows overall score per framework at a glance.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory.' },
    ],
    docsPath: '/spec#audit-coverage',
  },
  // ── Examiner exports (v0.16 — ADR-0061/0062) ──────────────────────────
  {
    id: 'nova-export-examiner-bagit',
    name: 'nova export-examiner bagit',
    journey: 'audit',
    description: 'Export a capsule as an RFC 8493 BagIt archive — suitable for digital preservation repositories (DSpace, Archivematica).',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory.' },
      { key: 'output', label: '--output', type: 'text', required: true, hint: 'Output path for the BagIt ZIP.' },
    ],
    docsPath: '/spec#export-examiner',
    nativeTabNote: 'BagIt export is also available from the Compliance tab → "Examiner Export" panel.',
  },
  {
    id: 'nova-export-examiner-pccp',
    name: 'nova export-examiner pccp',
    journey: 'audit',
    description: 'Export an FDA Predetermined Change Control Plan (PCCP) package per 21 CFR 880 — for AI/ML-based SaMD.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory.' },
      { key: 'output', label: '--output', type: 'text', required: true, hint: 'Output path for the PCCP package.' },
      { key: 'format', label: '--format', type: 'select', options: ['zip', 'json'], defaultValue: 'zip', hint: 'Output format.' },
    ],
    docsPath: '/spec#export-examiner',
    nativeTabNote: 'PCCP export is also available from the Compliance tab → "Examiner Export" panel.',
  },
  {
    id: 'nova-export-examiner-iso42001',
    name: 'nova export-examiner iso42001',
    journey: 'audit',
    description: 'Export an ISO/IEC 42001:2023 AI Management System evidence package — maps clause-level controls to NovaFabric evidence.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory (or data directory for period-based export).' },
      { key: 'output', label: '--output', type: 'text', required: true, hint: 'Output path for the ISO 42001 package ZIP.' },
    ],
    docsPath: '/spec#export-examiner',
    nativeTabNote: 'ISO 42001 export is also available from the Compliance tab → "Examiner Export" panel.',
  },
  {
    id: 'nova-export-annex-iv',
    name: 'nova export-annex-iv',
    journey: 'audit',
    description: 'Generate EU AI Act Annex IV technical documentation — produces all 15 mandatory elements as JSON-LD.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory.' },
      { key: 'output-dir', label: '--output-dir', type: 'text', required: true, hint: 'Output directory for Annex IV documentation.' },
      { key: 'deployment-id', label: '--deployment-id', type: 'text', required: true, hint: 'Deployment identifier for the AI system.' },
      { key: 'pdf', label: '--pdf', type: 'toggle', hint: 'Also generate a PDF version of the Annex IV document.' },
    ],
    docsPath: '/spec#export-annex-iv',
    nativeTabNote: 'EU AI Act Annex IV export is also available from the Compliance tab.',
  },
  {
    id: 'nova-export-nis2',
    name: 'nova export-nis2',
    journey: 'audit',
    description: 'Generate a NIS2 incident report per Directive (EU) 2022/2555 Art. 23 — Phase 1 ≤24h, Phase 2 ≤72h, Phase 3 ≤1 month.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory.' },
      { key: 'output', label: '--output', type: 'text', required: true, hint: 'Output path for the NIS2 report.' },
      { key: 'incident-id', label: '--incident-id', type: 'text', required: true, hint: 'Incident identifier for cross-reference.' },
      { key: 'phase', label: '--phase', type: 'select', options: ['1', '2', '3'], defaultValue: '1', hint: 'NIS2 notification phase.' },
    ],
    docsPath: '/spec#export-nis2',
    nativeTabNote: 'NIS2 report export is also available from the Compliance tab.',
  },
  {
    id: 'nova-subject-proof',
    name: 'nova subject-proof',
    journey: 'audit',
    description: 'Generate a GDPR Art. 17 erasure proof for a data subject — HMAC-SHA256 lookup in the redaction index.',
    fields: [
      { key: 'subject-id', label: 'subject-id', type: 'text', required: true, positional: true, hint: 'Data subject identifier (e.g. user@example.com). Never stored — only an HMAC is queried.' },
      { key: 'output', label: '--output', type: 'text', hint: 'Output path for the proof JSON. Defaults to stdout.' },
    ],
    docsPath: '/spec#subject-proof',
    nativeTabNote: 'GDPR subject proof is also available from the Compliance tab.',
  },
  {
    id: 'nova-aibom-generate',
    name: 'nova aibom generate',
    journey: 'audit',
    description: 'Generate CycloneDX 1.7 AI-BOMs for one or all capsules — per-deployment automation for EU CRA compliance. Skips capsules that already have an aibom.json unless --force.',
    fields: [
      { key: 'capsule-dir', label: 'capsule-dir', type: 'text', positional: true, hint: 'A single capsule directory to generate an AIBOM for. Omit and use --all to batch-process every capsule.' },
      { key: 'all', label: '--all', type: 'toggle', hint: 'Generate aibom.json for every capsule under --capsules-dir that does not already have one.' },
      { key: 'capsules-dir', label: '--capsules-dir', type: 'text',
        visibleWhen: { field: 'all', value: 'true' },
        hint: 'Directory of capsules for --all. Default: $NOVAFABRIC_CAPSULE_DIR or $NOVAFABRIC_HOME/capsules.' },
      { key: 'force', label: '--force', type: 'toggle', hint: 'Regenerate aibom.json even if one already exists.' },
    ],
    docsPath: '/spec#aibom',
    nativeTabNote: 'AI-SBOM generation is also available from the Compliance tab → "Generate AI-SBOM" panel.',
  },
  {
    id: 'nova-run-lineage',
    name: 'nova run lineage',
    journey: 'audit',
    description: 'Show lineage edges attached to a specific run — lists provenance and dependency edges for the given run_id.',
    fields: [
      { key: 'run-id', label: 'run-id', type: 'text', required: true, positional: true, hint: 'The run_id whose lineage edges to show.' },
      { key: 'edge-types', label: '--edge-types', type: 'text', hint: 'Comma-separated edge type filter (e.g. PRODUCED_BY,DEPENDS_ON).' },
      { key: 'depth', label: '--depth', type: 'number', defaultValue: '3', hint: 'Maximum hops to traverse. Default: 3.' },
      { key: 'format', label: '--format', type: 'select', options: ['text', 'json'], defaultValue: 'text', hint: 'Output format.' },
    ],
    docsPath: '/spec#run-lineage',
    nativeTabNote: 'Run lineage edges are also available from the Runs tab → run detail, or in the Lineage tab → enter a run_id in any query panel.',
  },

  // ── Infrastructure ────────────────────────────────────────────────────
  {
    id: 'nova-api-proxy',
    name: 'nova api-proxy',
    journey: 'infra',
    description: 'Start a local HTTP proxy that forwards LLM API calls to an upstream provider while capturing every request/response as a Run Capsule.',
    fields: [
      { key: 'upstream-url', label: '--upstream-url', type: 'text', required: true, hint: 'The upstream LLM API base URL (e.g. https://api.openai.com).' },
      { key: 'port', label: '--port', type: 'number', defaultValue: '8877', hint: 'Local port to bind the proxy. Default: 8877.' },
      { key: 'host', label: '--host', type: 'text', defaultValue: '127.0.0.1', hint: 'Interface to bind. Default: 127.0.0.1.' },
    ],
    docsPath: '/spec#api-proxy',
  },
  {
    id: 'nova-mcp-proxy',
    name: 'nova mcp-proxy',
    journey: 'infra',
    description: 'Start a stdio-based MCP proxy that forwards tool exchanges to an upstream MCP server while capturing them into the active Run Capsule.',
    fields: [
      { key: 'upstream', label: 'upstream', type: 'text', required: true, positional: true, hint: 'Upstream MCP server command or socket path.' },
      { key: 'port', label: '--port', type: 'number', hint: 'Optional TCP port for the proxy. Default: stdio mode.' },
    ],
    docsPath: '/spec#mcp-proxy',
  },
  {
    id: 'nova-doctor',
    name: 'nova doctor',
    journey: 'infra',
    description: 'Run diagnostic checks on the local NovaFabric installation — verifies storage backend health, key store, OPA availability, and schema versions.',
    fields: [
      { key: 'check-storage', label: '--check-storage', type: 'toggle', hint: 'Also probe the configured storage backend (SQLite, Postgres, or object store).' },
      { key: 'format', label: '--format', type: 'select', options: ['text', 'json'], defaultValue: 'text', hint: 'Output format.' },
    ],
    docsPath: '/spec#doctor',
  },
  {
    id: 'nova-serve',
    name: 'nova serve',
    journey: 'infra',
    description: 'Start the local dashboard server (this UI). Requires novafabric[serve] extras. The token printed on startup must be entered in the Connect panel.',
    fields: [
      { key: 'port', label: '--port', type: 'number', defaultValue: '7477', hint: 'Port to bind. Default: 7477.' },
      { key: 'host', label: '--host', type: 'text', defaultValue: '127.0.0.1', hint: 'Interface to bind. Use 0.0.0.0 for LAN access.' },
      { key: 'db-path', label: '--db-path', type: 'text', hint: 'Path to the registry SQLite file. Default: ~/.novafabric/registry.db.' },
      { key: 'capsule-dir', label: '--capsule-dir', type: 'text', hint: 'Directory containing Run Capsule subdirectories. Default: ~/.novafabric/runs/.' },
      { key: 'experimental', label: '--experimental', type: 'toggle', defaultValue: 'true', hint: 'Required flag — acknowledges the dashboard is experimental.' },
    ],
    docsPath: '/spec#serve',
  },
  {
    id: 'nova-new-run-id',
    name: 'nova new-run-id',
    journey: 'infra',
    description: 'Print a fresh ULID for use as NOVAFABRIC_GLOBAL_RUN_ID — use this to pre-allocate a run ID before launching a distributed or multi-process capture.',
    fields: [],
    docsPath: '/spec#new-run-id',
  },
  {
    id: 'nova-capsule-delete',
    name: 'nova capsule delete',
    journey: 'infra',
    description: 'Delete a Run Capsule from disk and its lineage entries from the store. Blocked by any active legal hold on the registry.',
    fields: [
      { key: 'run-id', label: 'run-id', type: 'text', required: true, positional: true, hint: 'The run_id of the capsule to delete.' },
      { key: 'force', label: '--force', type: 'toggle', hint: 'Skip confirmation prompt. Use with caution — deletion is irreversible.' },
    ],
    docsPath: '/spec#capsule-delete',
  },
  {
    id: 'nova-db-migrate',
    name: 'nova db migrate-to-postgres',
    journey: 'infra',
    description: 'One-time migration from local SQLite registry to a Postgres-backed MetadataStore (server mode, ADR-0050). Requires novafabric[server].',
    fields: [
      { key: 'postgres-url', label: '--postgres-url', type: 'text', required: true, hint: 'Postgres DSN (e.g. postgresql://nova:pass@localhost:5432/nova). Can be set via NOVA_POSTGRES_URL env var.' },
      { key: 'db-path', label: '--db-path', type: 'text', hint: 'Source SQLite path. Default: ~/.novafabric/registry.db.' },
      { key: 'dry-run', label: '--dry-run', type: 'toggle', hint: 'Validate the migration plan without writing to Postgres.' },
    ],
    docsPath: '/spec#db-migrate',
  },
  {
    id: 'nova-login',
    name: 'nova login',
    journey: 'infra',
    description: 'Authenticate with a NovaFabric server instance using Device Authorization Grant (ADR-0018) — prints a URL to open in the browser.',
    fields: [
      { key: 'server-url', label: '--server-url', type: 'text', required: true, hint: 'URL of the NovaFabric server (e.g. https://nova.internal).' },
    ],
    docsPath: '/spec#login',
  },
  {
    id: 'nova-logout',
    name: 'nova logout',
    journey: 'infra',
    description: 'Revoke the local OIDC session token and remove cached credentials for a NovaFabric server.',
    fields: [
      { key: 'server-url', label: '--server-url', type: 'text', hint: 'URL of the NovaFabric server to log out from. Defaults to the last authenticated server.' },
    ],
    docsPath: '/spec#logout',
  },
  {
    id: 'nova-migrate',
    name: 'nova migrate',
    journey: 'infra',
    description: 'Migrate Run Capsules from an older format version to the current schema. Required when upgrading from v0.1.x to v1.0.',
    fields: [
      { key: 'capsule-path', label: 'capsule-path', type: 'text', required: true, positional: true, hint: 'Path to the capsule directory or a directory of capsules.' },
      { key: 'from-version', label: '--from-version', type: 'text', hint: 'Source format version (e.g. 0.1.0). Auto-detected if omitted.' },
      { key: 'to-version', label: '--to-version', type: 'text', hint: 'Target format version. Defaults to the current stable version.' },
    ],
    docsPath: '/spec#migrate',
  },
  {
    id: 'nova-rebuild-metadata-db',
    name: 'nova rebuild-metadata-db',
    journey: 'infra',
    description: 'Rebuild the local metadata SQLite database from Run Capsule directories on disk. Use after manual capsule moves or to recover from a corrupted registry.',
    fields: [
      { key: 'db-path', label: '--db-path', type: 'text', hint: 'Path to the registry SQLite file to rebuild. Default: ~/.novafabric/registry.db.' },
      { key: 'dry-run', label: '--dry-run', type: 'toggle', hint: 'Scan capsules and report what would be imported without writing to the database.' },
    ],
    docsPath: '/spec#rebuild-metadata-db',
  },
  {
    id: 'nova-ingest-capsule',
    name: 'nova ingest-capsule',
    journey: 'infra',
    description: 'Index a capsule from disk into the runs metadata store — pass a run_id for a single capsule or --all to re-index every capsule in the capsule directory.',
    fields: [
      { key: 'run-id', label: 'run-id', type: 'text', positional: true, hint: 'Run ID to ingest. Omit when using --all.' },
      { key: 'all', label: '--all', type: 'toggle', hint: 'Re-index all capsules in the capsule directory.' },
      { key: 'capsule-dir', label: '--capsule-dir', type: 'text', hint: 'Override NOVAFABRIC_CAPSULE_DIR.' },
      { key: 'db-path', label: '--db-path', type: 'text', hint: 'Override NOVAFABRIC_DB_PATH.' },
    ],
    docsPath: '/spec#ingest-capsule',
    nativeTabNote: 'Capsule reindexing is also available from the Admin tab → "Reindex Capsules" panel.',
  },
  {
    id: 'nova-server-start',
    name: 'nova server start',
    journey: 'infra',
    description: 'Start the NovaFabric REST API server (server mode, ADR-0027). Requires novafabric[server]. Use "nova serve" to start the dashboard with the local-mode server bundled.',
    fields: [
      { key: 'host', label: '--host', type: 'text', defaultValue: '127.0.0.1', hint: 'Interface to bind. Use 0.0.0.0 for LAN/container access.' },
      { key: 'port', label: '--port', type: 'number', defaultValue: '7477', hint: 'Port to bind. Default: 7477.' },
      { key: 'postgres-url', label: '--postgres-url', type: 'text', hint: 'Postgres DSN for server-mode MetadataStore (e.g. postgresql://nova:pass@localhost:5432/nova). Falls back to SQLite if omitted.' },
      { key: 'oidc-issuer', label: '--oidc-issuer', type: 'text', hint: 'OIDC issuer URL for JWT validation (e.g. https://accounts.google.com). Required for multi-user auth.' },
      { key: 'dev', label: '--dev', type: 'toggle', hint: 'Enable development mode — disables auth, uses SQLite, and auto-reloads on code changes. Never use in production.' },
    ],
    docsPath: '/spec#server-start',
    nativeTabNote: 'You can also start the combined dashboard+server with "nova serve --experimental".',
  },
  {
    id: 'nova-server-flush-jwks-cache',
    name: 'nova server flush-jwks-cache',
    journey: 'infra',
    description: 'Force the running NovaFabric server to re-fetch its JWKS from the OIDC provider. Use after rotating OIDC signing keys or when auth failures suggest a stale key cache.',
    fields: [
      { key: 'server-url', label: '--server-url', type: 'text', hint: 'URL of the target NovaFabric server. Defaults to the last authenticated server.' },
    ],
    docsPath: '/spec#server-flush-jwks-cache',
    nativeTabNote: 'JWKS cache flush is also available from the Admin tab → "OIDC Configuration" section.',
  },
  {
    id: 'nova-lineage-store-migrate',
    name: 'nova lineage-store migrate',
    journey: 'infra',
    description: 'Migrate lineage edges between store backends (e.g. SQLite → KuzuDB or Postgres). Part of the Phase 6 lineage-at-scale migration kit.',
    fields: [
      { key: 'source', label: '--source', type: 'select', options: ['sqlite', 'postgres', 'kuzudb'], defaultValue: 'sqlite', hint: 'Source backend to migrate from.' },
      { key: 'target', label: '--target', type: 'select', options: ['sqlite', 'postgres', 'kuzudb'], defaultValue: 'kuzudb', hint: 'Target backend to migrate to.' },
      { key: 'dry-run', label: '--dry-run', type: 'toggle', hint: 'Count edges and validate compatibility without writing to the target.' },
    ],
    docsPath: '/spec#lineage-store-migrate',
  },
  {
    id: 'nova-lineage-store-profile',
    name: 'nova lineage-store profile',
    journey: 'infra',
    description: 'Print a ready-to-use docker-compose deployment profile for a lineage store backend — KuzuDB vertical single-node or JanusGraph + Cassandra minimal cluster.',
    fields: [
      { key: 'target', label: '--target', type: 'select', options: ['kuzudb-vertical', 'janusgraph-minimal'], defaultValue: 'kuzudb-vertical', hint: 'Profile target backend.' },
      { key: 'node-size', label: '--node-size', type: 'text', defaultValue: '16g-ram-500g-nvme', hint: 'Node size tag for kuzudb-vertical.',
        visibleWhen: { field: 'target', value: 'kuzudb-vertical' } },
      { key: 'rf', label: '--rf', type: 'number', defaultValue: '3', hint: 'Cassandra replication factor for janusgraph-minimal. Use 1 for dev.',
        visibleWhen: { field: 'target', value: 'janusgraph-minimal' } },
      { key: 'image-tag', label: '--image-tag', type: 'text', defaultValue: 'latest', hint: 'Docker image tag.' },
    ],
    docsPath: '/spec#lineage-store-profile',
    nativeTabNote: 'Deployment profiles are also available from the Infra tab → "Lineage Store Deployment Profile" panel.',
  },
  {
    id: 'nova-asset-diff',
    name: 'nova asset diff',
    journey: 'infra',
    description: 'Diff two asset spec versions — compares schema, metadata fields, and capability declarations. This is a spec/definition diff, not a Run Capsule diff.',
    fields: [
      { key: 'asset-a', label: 'asset@version-a', type: 'text', required: true, positional: true, hint: 'First asset version (e.g. summarizer-v2@0.3.0).' },
      { key: 'asset-b', label: 'asset@version-b', type: 'text', required: true, positional: true, hint: 'Second asset version (e.g. summarizer-v2@0.4.1).' },
    ],
    docsPath: '/spec#asset-diff',
    nativeTabNote: 'Run Capsule diffs are in the Diff tab. This command diffs asset spec definitions, not run outputs.',
  },
  {
    id: 'nova-db-upgrade',
    name: 'nova db upgrade',
    journey: 'infra',
    description: 'Run Alembic schema migrations against the configured database. Upgrades to HEAD by default; pass --revision to upgrade to a specific migration target.',
    fields: [
      { key: 'revision', label: '--revision', type: 'text', defaultValue: 'head', hint: 'Alembic revision target. Default: head (latest). Use a specific hash to upgrade to a named migration.' },
      { key: 'db-path', label: '--db-path', type: 'text', hint: 'Override the database path. Default: the configured MetadataStore URL.' },
    ],
    docsPath: '/spec#db-upgrade',
  },
];

export const JOURNEY_LABELS: Record<Journey, string> = {
  debug: 'Debug & Investigate',
  govern: 'Govern & Promote',
  audit: 'Audit & Verify',
  infra: 'Infrastructure',
};

// ── Full CLI coverage ──────────────────────────────────────────────────────
// GENERATED_COMMANDS is auto-derived from the live Typer app by
// `web/scripts/gen-command-registry.py` and covers every `nova` command. The
// hand-curated defs above provide richer copy (hints, native-tab notes) for the
// most-used commands and WIN on collision. Every other command still appears in
// the CommandsTab as a generated form, so the dashboard mirrors the full CLI.
//
// To refresh after adding/removing a CLI command:
//   uv run python web/scripts/gen-command-registry.py
// A pytest guard (tests/dashboard/test_command_registry_coverage.py) fails CI if
// any CLI command is missing from this registry.

const _curatedNames = new Set(CURATED_COMMANDS.map((c) => c.name));

/**
 * The complete command set shown in the dashboard: every hand-curated def, plus
 * a generated def for each remaining `nova` command not covered by a curated one.
 */
export const COMMANDS: readonly CommandDef[] = [
  ...CURATED_COMMANDS,
  ...GENERATED_COMMANDS.filter((c) => !_curatedNames.has(c.name)),
];

/** The "family" of a command — its first token after `nova` (e.g. `seal`, `kg`),
 *  or the command name itself for top-level single commands. Used to sub-group
 *  the long command list in the sidebar. */
export function commandFamily(cmd: CommandDef): string {
  const parts = cmd.name.split(' '); // ['nova', 'seal', 'sign']
  return parts.length > 2 ? parts[1] : parts[1] ?? '';
}
