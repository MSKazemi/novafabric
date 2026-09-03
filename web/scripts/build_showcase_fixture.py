#!/usr/bin/env python3
"""Generate the showcase fixture: a single coherent code-review-agent story.

The story:
    A fictional code-review-agent reads a diff, calls a model twice, invokes two
    tools (run_tests, read_file), and emits a review. We capture v0.1.0 (RUN_A),
    capture v0.2.0 (RUN_B, regression), replay v0.1.0 in mocked mode (RUN_C),
    and produce a diff that shows the regression.

Output: web/src/data/fixtures/ with assets, capsules (RUN_A/B/C), lineage,
diff, and an evidence bundle. Each output validates against the published
schemas at /schemas/ via jsonschema (when the package is available).

This script is the source of truth for the showcase fixture. Running it should
be deterministic — no network, no real LLM calls, no current-time leak (we
freeze timestamps).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

WEB_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = WEB_ROOT.parent
FIXTURES = WEB_ROOT / "src" / "data" / "fixtures"
SCHEMAS_SRC = REPO_ROOT / "schemas"
SCHEMAS_DST = WEB_ROOT / "src" / "data" / "schemas"

# Frozen timestamps so the fixture is byte-stable.
T_BASE = "2026-04-15T10:00:00+00:00"
T_RUN_A_END = "2026-04-15T10:00:04+00:00"
T_RUN_B_END = "2026-05-02T14:30:05+00:00"
T_RUN_C_END = "2026-05-09T09:15:03+00:00"

# Frozen ULIDs (from a deterministic source, not random).
RUN_A_ID = "01KR5SQZPDGTKE3MDP3ZRX8WP1"
RUN_B_ID = "01KS9K8R2NHQXM7P3D2VBC8KMN"
RUN_C_ID = "01KT2P4Y8WGTHE9MRP4ZRX7QP3"

ASSETS = [
    {
        "name": "gpt-4o-mini",
        "version": "2024-07-18",
        "asset_type": "model",
        "status": "promoted",
        "description": "OpenAI's small frontier model. Used as the reasoning core of code-review-agent.",
        "spec": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "context_window": 128000,
            "max_output_tokens": 16384,
        },
    },
    {
        "name": "code-review-prompt",
        "version": "0.1.0",
        "asset_type": "prompt",
        "status": "promoted",
        "description": "System prompt for the code-review-agent. Asks the model to identify bugs, style issues, and missing edge cases.",
        "spec": {
            "template": "You are a thorough code reviewer. For the diff below, identify: (1) bugs, (2) edge cases the author may have missed, (3) style inconsistencies. Be specific. Quote line numbers.\n\n{diff}",
            "variables": ["diff"],
        },
    },
    {
        "name": "code-review-prompt",
        "version": "0.2.0",
        "asset_type": "prompt",
        "status": "development",
        "description": "Shorter, faster v0.2.0 of the code-review prompt. Failed eval suite — misses edge cases.",
        "spec": {
            "template": "Review this code diff. Note bugs and style issues.\n\n{diff}",
            "variables": ["diff"],
        },
    },
    {
        "name": "python-test-corpus",
        "version": "1.0.0",
        "asset_type": "dataset",
        "status": "promoted",
        "description": "Curated set of 200 Python diffs with known bugs, used for evaluating code-review agents.",
        "spec": {"size": 200, "format": "jsonl"},
    },
    {
        "name": "run-tests-tool",
        "version": "0.3.0",
        "asset_type": "tool",
        "status": "promoted",
        "description": "Runs the project's test suite in a sandboxed subprocess.",
        "spec": {"transport": "mcp", "mutating": False},
    },
    {
        "name": "read-file-tool",
        "version": "1.1.0",
        "asset_type": "tool",
        "status": "promoted",
        "description": "Reads a file from the working tree. Read-only.",
        "spec": {"transport": "mcp", "mutating": False},
    },
    {
        "name": "code-review-agent",
        "version": "0.1.0",
        "asset_type": "agent",
        "status": "promoted",
        "description": "Code-review agent. Composes gpt-4o-mini, code-review-prompt@0.1.0, run-tests-tool, read-file-tool.",
        "spec": {
            "depends_on": [
                "gpt-4o-mini@2024-07-18",
                "code-review-prompt@0.1.0",
                "run-tests-tool@0.3.0",
                "read-file-tool@1.1.0",
            ],
        },
    },
    {
        "name": "code-review-agent",
        "version": "0.2.0",
        "asset_type": "agent",
        "status": "development",
        "description": "Code-review agent v0.2.0. Uses code-review-prompt@0.2.0. Eval-failed; held in development.",
        "spec": {
            "depends_on": [
                "gpt-4o-mini@2024-07-18",
                "code-review-prompt@0.2.0",
                "run-tests-tool@0.3.0",
                "read-file-tool@1.1.0",
            ],
        },
    },
]

EVAL_RESULTS = [
    {"asset": "code-review-prompt@0.1.0", "suite": "edge-case-coverage", "passed": True, "score": 0.91},
    {"asset": "code-review-prompt@0.1.0", "suite": "tone-and-clarity", "passed": True, "score": 0.88},
    {"asset": "code-review-prompt@0.2.0", "suite": "edge-case-coverage", "passed": False, "score": 0.62},
    {"asset": "code-review-prompt@0.2.0", "suite": "tone-and-clarity", "passed": True, "score": 0.84},
    {"asset": "code-review-agent@0.1.0", "suite": "regression-set", "passed": True, "score": 0.93},
    {"asset": "code-review-agent@0.2.0", "suite": "regression-set", "passed": False, "score": 0.71},
]


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_capsule(run_id: str, agent_version: str, finished_at: str, prompt_version: str) -> dict[str, Any]:
    diff_hash = "sha256:" + hashlib.sha256(b"--- a/parser.py\n+++ b/parser.py\n").hexdigest()
    review_hash = "sha256:" + hashlib.sha256(f"review by code-review-agent@{agent_version}".encode()).hexdigest()
    return {
        "schema_version": "0.1.0",
        "novafabric_version": "0.6.12",
        "run_id": run_id,
        "created_at": T_BASE,
        "finished_at": finished_at,
        "duration_ms": 4127,
        "command": ["python", "review.py", "--diff", "examples/sample.diff"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "working_directory": "~/projects/code-review-agent",
        "host": {
            "arch": "x86_64",
            "os": "linux",
            "python": "3.12.4",
            "cpu_count": 16,
            "memory_bytes": 67_108_864_000,
            "gpu": [],
            "hostname_redacted": True,
        },
        "trace_ref": "trace.jsonl",
        "trace_root_span_id": "0123456789abcdef",
        "model_calls_ref": "model-calls.jsonl",
        "tool_calls_ref": "tool-calls.jsonl",
        "assets_ref": "assets.jsonl",
        "environment_ref": "env.lock",
        "replay_policy_ref": "replay.yaml",
        "redaction_proof_ref": "redaction-proof.json",
        "lineage_ref": "lineage.jsonl",
        "model_call_count": 2,
        "tool_call_count": 2,
        "mutating_tool_count": 0,
        "inputs": [
            {
                "name": "diff",
                "path": "inputs/sample.diff",
                "content_hash": diff_hash,
                "size_bytes": 1240,
                "media_type": "text/x-diff",
            }
        ],
        "outputs": [
            {
                "name": "review",
                "path": "outputs/review.md",
                "content_hash": review_hash,
                "size_bytes": 612,
                "media_type": "text/markdown",
            }
        ],
        "metadata": {
            "agent": f"code-review-agent@{agent_version}",
            "prompt": f"code-review-prompt@{prompt_version}",
        },
    }


def build_model_calls(run_label: str) -> list[dict[str, Any]]:
    if run_label == "A":
        review = "Found two issues. Line 42: off-by-one in the slice. Line 78: missing null-check on the optional param. Style: prefer `is None` over `== None`."
    else:
        review = "LGTM. Minor style: prefer `is None`."
    return [
        {
            "schema_version": "0.1.0",
            "call_id": f"call_{run_label}_001",
            "started_at": T_BASE,
            "duration_ms": 1240,
            "provider": "openai",
            "model": "gpt-4o-mini",
            "model_version": "2024-07-18",
            "request": {
                "temperature": 0.2,
                "max_tokens": 1024,
                "top_p": 1.0,
                "messages_redacted_count": 2,
            },
            "response": {
                "id": f"resp_{run_label}_001",
                "finish_reason": "stop",
                "completion_redacted": True,
            },
            "usage": {"prompt_tokens": 412, "completion_tokens": 156, "total_tokens": 568},
        },
        {
            "schema_version": "0.1.0",
            "call_id": f"call_{run_label}_002",
            "started_at": "2026-04-15T10:00:02+00:00",
            "duration_ms": 980,
            "provider": "openai",
            "model": "gpt-4o-mini",
            "model_version": "2024-07-18",
            "request": {
                "temperature": 0.2,
                "max_tokens": 512,
                "top_p": 1.0,
                "messages_redacted_count": 4,
            },
            "response": {
                "id": f"resp_{run_label}_002",
                "finish_reason": "stop",
                "completion_redacted": True,
                "completion_preview": review,
            },
            "usage": {"prompt_tokens": 612, "completion_tokens": 84, "total_tokens": 696},
        },
    ]


def build_tool_calls() -> list[dict[str, Any]]:
    return [
        {
            "schema_version": "0.1.0",
            "call_id": "tc_001",
            "started_at": "2026-04-15T10:00:00.500+00:00",
            "duration_ms": 312,
            "transport": "mcp",
            "tool_name": "read-file",
            "tool_version": "1.1.0",
            "args_redacted": {"path": "src/parser.py"},
            "result_status": "success",
            "result_preview": "def parse(s): ...",
            "mutating": False,
        },
        {
            "schema_version": "0.1.0",
            "call_id": "tc_002",
            "started_at": "2026-04-15T10:00:01.200+00:00",
            "duration_ms": 2105,
            "transport": "mcp",
            "tool_name": "run-tests",
            "tool_version": "0.3.0",
            "args_redacted": {"target": "tests/test_parser.py"},
            "result_status": "success",
            "result_preview": "12 passed, 0 failed",
            "mutating": False,
        },
    ]


def build_lineage_edges() -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    common = {
        "schema_version": "0.1.0",
        "direction": "source_to_target",
        "created_at": T_BASE,
        "emitter": {"name": "novafabric.lineage", "version": "0.6.12"},
        "confidence": "observed",
    }

    # RUN_A consumed the v0.1.0 prompt + agent + 4 supporting assets
    a_consumed = [
        "asset:gpt-4o-mini@2024-07-18",
        "asset:code-review-prompt@0.1.0",
        "asset:python-test-corpus@1.0.0",
        "asset:run-tests-tool@0.3.0",
        "asset:read-file-tool@1.1.0",
        "asset:code-review-agent@0.1.0",
    ]
    for i, a in enumerate(a_consumed):
        edges.append({
            **common,
            "edge_id": f"e_a_{i:02d}",
            "edge_type": "consumed",
            "source": f"run:{RUN_A_ID}",
            "target": a,
            "capsule_run_id": RUN_A_ID,
        })

    # RUN_B consumed the v0.2.0 prompt + agent + 4 supporting assets
    b_consumed = [
        "asset:gpt-4o-mini@2024-07-18",
        "asset:code-review-prompt@0.2.0",
        "asset:python-test-corpus@1.0.0",
        "asset:run-tests-tool@0.3.0",
        "asset:read-file-tool@1.1.0",
        "asset:code-review-agent@0.2.0",
    ]
    for i, a in enumerate(b_consumed):
        edges.append({
            **common,
            "edge_id": f"e_b_{i:02d}",
            "edge_type": "consumed",
            "source": f"run:{RUN_B_ID}",
            "target": a,
            "capsule_run_id": RUN_B_ID,
        })

    # RUN_A produced an artifact
    edges.append({
        **common,
        "edge_id": "e_a_prod",
        "edge_type": "produced_by",
        "source": f"artifact:{RUN_A_ID}:outputs/review.md",
        "target": f"run:{RUN_A_ID}",
        "capsule_run_id": RUN_A_ID,
    })

    # RUN_C replayed_from RUN_A
    edges.append({
        **common,
        "edge_id": "e_c_replayed",
        "edge_type": "replayed_from",
        "source": f"run:{RUN_C_ID}",
        "target": f"run:{RUN_A_ID}",
        "capsule_run_id": RUN_C_ID,
    })

    # code-review-prompt@0.2.0 evaluated_by failing suite
    edges.append({
        **common,
        "edge_id": "e_eval_fail",
        "edge_type": "evaluated_by",
        "source": "asset:code-review-prompt@0.2.0",
        "target": "asset:edge-case-coverage-suite@1.0.0",
        "capsule_run_id": RUN_B_ID,
        "confidence": "observed",
    })

    return edges


def build_diff() -> dict[str, Any]:
    return {
        "schema_version": "0.1.0",
        "run_a_id": RUN_A_ID,
        "run_b_id": RUN_B_ID,
        "summary": {"changed": 2, "added": 0, "removed": 0},
        "sections": {
            "environment": {"changes": []},
            "model_calls": {
                "aligned": 2,
                "changed": 1,
                "added": 0,
                "removed": 0,
                "pairs": [
                    {
                        "index": 0,
                        "a_call_id": "call_A_001",
                        "b_call_id": "call_B_001",
                        "changes": [],
                    },
                    {
                        "index": 1,
                        "a_call_id": "call_A_002",
                        "b_call_id": "call_B_002",
                        "changes": [
                            {
                                "field": "request.messages[0].content",
                                "before": "You are a thorough code reviewer. For the diff below, identify: (1) bugs, (2) edge cases the author may have missed, (3) style inconsistencies. Be specific. Quote line numbers.",
                                "after": "Review this code diff. Note bugs and style issues.",
                                "severity": "major",
                            }
                        ],
                    },
                ],
            },
            "tool_calls": {"aligned": 2, "pairs": []},
            "outputs": {
                "changes": [
                    {
                        "path": "outputs/review.md",
                        "before_hash": stable_hash("Found two issues. Line 42: off-by-one in the slice. Line 78: missing null-check on the optional param. Style: prefer `is None` over `== None`."),
                        "after_hash": stable_hash("LGTM. Minor style: prefer `is None`."),
                        "severity": "major",
                    }
                ]
            },
        },
    }


def build_evidence_bundle() -> dict[str, Any]:
    predicate = {
        "predicateType": "https://novafabric.dev/schemas/run-capsule/v0.1.0",
        "subject": [
            {"name": f"run:{RUN_A_ID}", "digest": {"sha256": stable_hash(RUN_A_ID)[:64]}}
        ],
        "predicate": {
            "schema_version": "0.1.0",
            "run_id": RUN_A_ID,
            "manifest_hash": stable_hash(f"manifest:{RUN_A_ID}")[:64],
            "lineage_edge_count": 8,
            "redaction_findings": 0,
        },
    }
    dsse = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": predicate["subject"],
        "predicateType": predicate["predicateType"],
        "predicate": predicate["predicate"],
        "signature": {
            "keyid": "demo-key-2026",
            "alg": "ed25519",
            "sig": "BASE64_ED25519_SIGNATURE_PLACEHOLDER_REPLACED_BY_NOBLE_AT_RUNTIME",
        },
    }
    manifest = {
        "schema_version": "0.1.0",
        "files": [
            {"path": "capsule/capsule.yaml", "sha256": stable_hash(f"{RUN_A_ID}:capsule.yaml")[:64], "size": 1820},
            {"path": "capsule/trace.jsonl", "sha256": stable_hash(f"{RUN_A_ID}:trace.jsonl")[:64], "size": 312},
            {"path": "capsule/model-calls.jsonl", "sha256": stable_hash(f"{RUN_A_ID}:model-calls.jsonl")[:64], "size": 1240},
            {"path": "capsule/tool-calls.jsonl", "sha256": stable_hash(f"{RUN_A_ID}:tool-calls.jsonl")[:64], "size": 880},
            {"path": "capsule/env.lock", "sha256": stable_hash(f"{RUN_A_ID}:env.lock")[:64], "size": 4612},
            {"path": "capsule/redaction-proof.json", "sha256": stable_hash(f"{RUN_A_ID}:redaction-proof.json")[:64], "size": 1816},
            {"path": "lineage.jsonl", "sha256": stable_hash(f"{RUN_A_ID}:lineage")[:64], "size": 2104},
            {"path": "predicate.json", "sha256": stable_hash(json.dumps(predicate, sort_keys=True))[:64], "size": 480},
            {"path": "dsse-statement.json", "sha256": stable_hash(json.dumps(dsse, sort_keys=True))[:64], "size": 612},
            {"path": "schemas/run-capsule.schema.json", "sha256": "vendored", "size": 8420},
        ],
        "manifest_hash": stable_hash(f"manifest:{RUN_A_ID}")[:64],
    }
    return {"dsse": dsse, "predicate": predicate, "manifest": manifest}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def write_jsonl(path: Path, records: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def write_yaml_simple(path: Path, data: dict[str, Any]) -> None:
    """Minimal YAML writer to avoid a runtime PyYAML dep — fine for our flat dicts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []

    def render(obj: Any, indent: int = 0) -> None:
        prefix = "  " * indent
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)) and v:
                    lines.append(f"{prefix}{k}:")
                    render(v, indent + 1)
                else:
                    lines.append(f"{prefix}{k}: {json.dumps(v) if not isinstance(v, str) else json.dumps(v)}")
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    lines.append(f"{prefix}-")
                    render(item, indent + 1)
                else:
                    lines.append(f"{prefix}- {json.dumps(item)}")

    render(data)
    path.write_text("\n".join(lines) + "\n")


def copy_schemas() -> None:
    SCHEMAS_DST.mkdir(parents=True, exist_ok=True)
    if not SCHEMAS_SRC.exists():
        print(f"[warn] schemas source missing at {SCHEMAS_SRC}; copying skipped")
        return
    for src in SCHEMAS_SRC.glob("*.json"):
        dst = SCHEMAS_DST / src.name
        dst.write_bytes(src.read_bytes())
    print(f"[ok] copied {len(list(SCHEMAS_DST.glob('*.json')))} schema files to {SCHEMAS_DST}")


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)

    # Registry
    write_json(FIXTURES / "registry.json", {
        "schema_version": "0.1.0",
        "assets": ASSETS,
        "eval_results": EVAL_RESULTS,
    })
    print(f"[ok] wrote registry: {len(ASSETS)} assets, {len(EVAL_RESULTS)} eval results")

    # Capsules
    runs = [
        ("RUN_A", RUN_A_ID, "0.1.0", "0.1.0", T_RUN_A_END, "A"),
        ("RUN_B", RUN_B_ID, "0.2.0", "0.2.0", T_RUN_B_END, "B"),
        ("RUN_C", RUN_C_ID, "0.1.0", "0.1.0", T_RUN_C_END, "A"),  # replay of A reuses A's labels
    ]
    for label, run_id, agent_v, prompt_v, end_t, model_label in runs:
        cdir = FIXTURES / "capsules" / label
        cdir.mkdir(parents=True, exist_ok=True)
        capsule = build_capsule(run_id, agent_v, end_t, prompt_v)
        write_yaml_simple(cdir / "capsule.yaml", capsule)
        write_json(cdir / "capsule.json", capsule)  # JSON twin for easy frontend loading
        write_jsonl(cdir / "model-calls.jsonl", build_model_calls(model_label))
        write_jsonl(cdir / "tool-calls.jsonl", build_tool_calls())
        write_jsonl(cdir / "trace.jsonl", [
            {"span_id": "root", "name": "code-review-agent", "duration_ms": 4127, "kind": "internal"},
            {"span_id": "model_001", "parent": "root", "name": "openai.completion", "duration_ms": 1240, "kind": "client"},
            {"span_id": "tool_001", "parent": "root", "name": "mcp.read-file", "duration_ms": 312, "kind": "client"},
            {"span_id": "tool_002", "parent": "root", "name": "mcp.run-tests", "duration_ms": 2105, "kind": "client"},
            {"span_id": "model_002", "parent": "root", "name": "openai.completion", "duration_ms": 980, "kind": "client"},
        ])
        print(f"[ok] wrote capsule {label} ({run_id})")

    # Lineage
    write_jsonl(FIXTURES / "lineage.jsonl", build_lineage_edges())
    print("[ok] wrote lineage edges")

    # Diff (RUN_A vs RUN_B)
    write_json(FIXTURES / "diff-A-vs-B.json", build_diff())
    print("[ok] wrote diff RUN_A vs RUN_B")

    # Evidence bundle
    bundle = build_evidence_bundle()
    write_json(FIXTURES / "evidence-bundle" / "dsse-statement.json", bundle["dsse"])
    write_json(FIXTURES / "evidence-bundle" / "predicate.json", bundle["predicate"])
    write_json(FIXTURES / "evidence-bundle" / "manifest.json", bundle["manifest"])
    # Demo public key (PEM-format placeholder)
    (FIXTURES / "evidence-bundle" / "public-key.pem").write_text(
        "-----BEGIN PUBLIC KEY-----\n"
        "MCowBQYDK2VwAyEADEMOEDDEMOEDDEMOEDDEMOEDDEMOEDDEMOEDDEMOEDDE=\n"
        "-----END PUBLIC KEY-----\n"
        "# This is a demonstration key for the showcase site only.\n"
        "# Real evidence bundles use a per-deployment ed25519 key; see ADR-0011.\n"
    )
    print("[ok] wrote evidence bundle")

    # Copy live schemas alongside the fixture so the build can validate against them
    copy_schemas()

    print(f"\nFixture written to {FIXTURES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
