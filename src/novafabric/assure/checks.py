from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml

from novafabric.assure.models import AssuranceResult, CheckStatus

_TOOL_RATIO_WARN = 10   # tool_calls per model_call
_TOOL_RATIO_FAIL = 50
_DURATION_WARN_MS = 1_800_000   # 30 minutes
_DURATION_FAIL_MS = 3_600_000   # 1 hour
_TOKEN_WARN = 100_000


class BaseCheck(ABC):
    check_id: str
    category: str

    @abstractmethod
    def run(self, capsule_dir: Path) -> AssuranceResult:
        ...

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            return {}
        return raw

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {}
        return raw

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        lines = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return lines

    def _pass(
        self, msg: str, evidence: dict[str, Any] | None = None
    ) -> AssuranceResult:
        return AssuranceResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASS,
            message=msg,
            evidence=evidence or {},
        )

    def _fail(
        self, msg: str, evidence: dict[str, Any] | None = None
    ) -> AssuranceResult:
        return AssuranceResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.FAIL,
            message=msg,
            evidence=evidence or {},
        )

    def _warn(
        self, msg: str, evidence: dict[str, Any] | None = None
    ) -> AssuranceResult:
        return AssuranceResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.WARN,
            message=msg,
            evidence=evidence or {},
        )

    def _skip(self, msg: str) -> AssuranceResult:
        return AssuranceResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.SKIP,
            message=msg,
        )


class LLM01Check(BaseCheck):
    check_id = "LLM01"
    category = "LLM01-PromptInjection"

    def run(self, capsule_dir: Path) -> AssuranceResult:
        proof_path = capsule_dir / "redaction-proof.json"
        if not proof_path.exists():
            return self._fail(
                "redaction-proof.json missing — prompt injection scanning"
                " was not performed",
                {"missing": "redaction-proof.json"},
            )
        return self._pass(
            "Redaction proof present — secret scanning executed before capsule write",
            {"proof_file": "redaction-proof.json"},
        )


class LLM02Check(BaseCheck):
    check_id = "LLM02"
    category = "LLM02-SensitiveInfoDisclosure"

    def run(self, capsule_dir: Path) -> AssuranceResult:
        proof_path = capsule_dir / "redaction-proof.json"
        if not proof_path.exists():
            return self._skip(
                "redaction-proof.json missing — cannot assess secret disclosure"
            )
        proof = self._read_json(proof_path)
        found = proof.get("secrets_found", 0)
        if found > 0:
            return self._fail(
                f"{found} secret(s) detected in capsule"
                " — sensitive information may be disclosed",
                {
                    "secrets_found": found,
                    "redacted_count": len(proof.get("redacted", [])),
                },
            )
        return self._pass(
            "No secrets detected in capsule artifacts",
            {"secrets_found": 0},
        )


class LLM03Check(BaseCheck):
    check_id = "LLM03"
    category = "LLM03-SupplyChain"

    def run(self, capsule_dir: Path) -> AssuranceResult:
        env_path = capsule_dir / "env.lock"
        if not env_path.exists():
            return self._fail(
                "env.lock missing — runtime environment not captured;"
                " supply-chain traceability absent",
                {"missing": "env.lock"},
            )
        manifest = self._read_yaml(capsule_dir / "capsule.yaml")
        nf_version = manifest.get("novafabric_version", "")
        if not nf_version:
            return self._warn(
                "env.lock present but novafabric_version absent from capsule.yaml",
                {"env_lock": True, "novafabric_version": None},
            )
        return self._pass(
            f"env.lock captured + novafabric_version={nf_version} recorded",
            {"novafabric_version": nf_version},
        )


class LLM04Check(BaseCheck):
    check_id = "LLM04"
    category = "LLM04-DataModelPoisoning"

    def run(self, capsule_dir: Path) -> AssuranceResult:
        calls = self._read_jsonl(capsule_dir / "model-calls.jsonl")
        if not calls:
            return self._skip(
                "model-calls.jsonl empty or absent — cannot assess token bounds"
            )
        total_input = sum(
            c.get("tokens", {}).get("input", 0) for c in calls if isinstance(c, dict)
        )
        if total_input > _TOKEN_WARN:
            return self._warn(
                f"Total input tokens {total_input} exceeds warning threshold"
                f" {_TOKEN_WARN} — potential data poisoning via prompt flooding",
                {"total_input_tokens": total_input, "threshold": _TOKEN_WARN},
            )
        return self._pass(
            f"Token counts within bounds (total input: {total_input})",
            {"total_input_tokens": total_input, "model_call_count": len(calls)},
        )


class LLM05Check(BaseCheck):
    check_id = "LLM05"
    category = "LLM05-ImproperOutputHandling"

    def run(self, capsule_dir: Path) -> AssuranceResult:
        tool_path = capsule_dir / "tool-calls.jsonl"
        manifest = self._read_yaml(capsule_dir / "capsule.yaml")
        tool_count = manifest.get("tool_call_count", 0)
        if tool_count == 0:
            return self._pass(
                "No tool calls recorded — improper output handling not applicable",
                {"tool_call_count": 0},
            )
        if not tool_path.exists():
            return self._fail(
                f"tool-calls.jsonl missing despite"
                f" tool_call_count={tool_count} in manifest",
                {"tool_call_count": tool_count, "missing": "tool-calls.jsonl"},
            )
        return self._pass(
            f"Tool call outputs captured in tool-calls.jsonl ({tool_count} calls)",
            {"tool_call_count": tool_count},
        )


class LLM06Check(BaseCheck):
    check_id = "LLM06"
    category = "LLM06-ExcessiveAgency"

    def run(self, capsule_dir: Path) -> AssuranceResult:
        manifest = self._read_yaml(capsule_dir / "capsule.yaml")
        model_calls = manifest.get("model_call_count", 0)
        tool_calls = manifest.get("tool_call_count", 0)
        mutating = manifest.get("mutating_tool_count", 0)
        if model_calls == 0:
            return self._skip("No model calls recorded — cannot compute tool ratio")
        ratio = tool_calls / model_calls
        if ratio >= _TOOL_RATIO_FAIL:
            return self._fail(
                f"Excessive agency: {tool_calls} tool calls per {model_calls}"
                f" model calls (ratio {ratio:.1f} >= {_TOOL_RATIO_FAIL})",
                {"tool_ratio": ratio, "mutating_tools": mutating},
            )
        if ratio >= _TOOL_RATIO_WARN:
            return self._warn(
                f"High agency: tool/model ratio {ratio:.1f}"
                f" >= {_TOOL_RATIO_WARN} warning threshold",
                {"tool_ratio": ratio, "mutating_tools": mutating},
            )
        return self._pass(
            f"Tool/model ratio {ratio:.1f} within acceptable range",
            {
                "tool_ratio": ratio,
                "tool_calls": tool_calls,
                "model_calls": model_calls,
            },
        )


class LLM07Check(BaseCheck):
    check_id = "LLM07"
    category = "LLM07-SystemPromptLeakage"

    def run(self, capsule_dir: Path) -> AssuranceResult:
        calls = self._read_jsonl(capsule_dir / "model-calls.jsonl")
        leaked = [
            c
            for c in calls
            if isinstance(c, dict)
            and c.get("level", "INFO") == "DEBUG"
            and "system_prompt" in c
        ]
        if leaked:
            return self._fail(
                f"System prompt found in {len(leaked)} model-call DEBUG"
                " record(s) — potential leakage",
                {"leaked_records": len(leaked)},
            )
        return self._pass(
            "No system prompt fields found in model-calls.jsonl records",
            {"records_checked": len(calls)},
        )


class LLM08Check(BaseCheck):
    check_id = "LLM08"
    category = "LLM08-VectorEmbeddingWeakness"

    def run(self, capsule_dir: Path) -> AssuranceResult:
        replay_path = capsule_dir / "replay.yaml"
        if not replay_path.exists():
            return self._fail(
                "replay.yaml missing — no replay policy captured;"
                " embedding/retrieval reproducibility unknown",
                {"missing": "replay.yaml"},
            )
        policy = self._read_yaml(replay_path)
        mode = policy.get("mode", "unknown")
        return self._pass(
            f"Replay policy captured (mode={mode}) — retrieval inputs are reproducible",
            {"replay_mode": mode},
        )


class LLM09Check(BaseCheck):
    check_id = "LLM09"
    category = "LLM09-Misinformation"

    def run(self, capsule_dir: Path) -> AssuranceResult:
        spans = self._read_jsonl(capsule_dir / "trace.jsonl")
        if not spans:
            return self._fail(
                "trace.jsonl empty or absent — execution provenance not recorded;"
                " misinformation traceability absent",
                {"span_count": 0},
            )
        error_spans = [
            s for s in spans if isinstance(s, dict) and s.get("status") == "error"
        ]
        return self._pass(
            f"Trace spans recorded ({len(spans)} spans, {len(error_spans)} errors)",
            {"span_count": len(spans), "error_spans": len(error_spans)},
        )


class LLM10Check(BaseCheck):
    check_id = "LLM10"
    category = "LLM10-UnboundedConsumption"

    def run(self, capsule_dir: Path) -> AssuranceResult:
        manifest = self._read_yaml(capsule_dir / "capsule.yaml")
        duration_ms = manifest.get("duration_ms", 0)
        if duration_ms >= _DURATION_FAIL_MS:
            return self._fail(
                f"Run duration {duration_ms}ms exceeds 1-hour gate"
                f" ({_DURATION_FAIL_MS}ms) — potential unbounded consumption",
                {"duration_ms": duration_ms, "threshold_ms": _DURATION_FAIL_MS},
            )
        if duration_ms >= _DURATION_WARN_MS:
            return self._warn(
                f"Run duration {duration_ms}ms exceeds 30-minute warning"
                f" ({_DURATION_WARN_MS}ms)",
                {"duration_ms": duration_ms, "threshold_ms": _DURATION_WARN_MS},
            )
        return self._pass(
            f"Run duration {duration_ms}ms within bounds",
            {"duration_ms": duration_ms},
        )


ALL_CHECKS: list[BaseCheck] = [
    LLM01Check(),
    LLM02Check(),
    LLM03Check(),
    LLM04Check(),
    LLM05Check(),
    LLM06Check(),
    LLM07Check(),
    LLM08Check(),
    LLM09Check(),
    LLM10Check(),
]
