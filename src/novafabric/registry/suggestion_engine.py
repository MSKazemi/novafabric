"""Asset registration suggestion engine.

Analyzes captured capsule data (model-calls.jsonl, tool-calls.jsonl, capsule.yaml)
and produces registration suggestions for assets that were observed but not yet
in the registry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from novafabric._capsule_fs import iter_capsule_dirs, read_capsule_text_or_none
from novafabric.capture.record_roles import read_logical_model_calls

_SEMVER_RE = re.compile(
    r"^v?\d+\.\d+(\.\d+)?(-[a-zA-Z0-9.]+)?(\+[a-zA-Z0-9.]+)?$"
)

# YAML plain-scalar indicator characters that must be quoted when leading.
# Includes '@', '`', and the flow/block indicators listed in the YAML 1.2 spec.
# A value of these characters only is a plain YAML scalar of the same text.
_YAML_PLAIN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+/-]*")


def _read_stream(capsule_dir: Path, name: str) -> str:
    """A capsule file's text, or ``""`` when it is missing, a link or not a regular file."""
    text = read_capsule_text_or_none(capsule_dir, name, operation="suggest-register")
    return text or ""


def _yaml_str(value: str) -> str:
    """Return *value* as a YAML scalar that parses back to exactly that string.

    Plain when it is made of ordinary name characters, else a double-quoted
    scalar (JSON string syntax is valid YAML) with newlines, quotes and
    backslashes escaped. Names, versions and tool names come from a workload or
    an LLM; interpolated raw, a newline added keys to the spec the user then
    registered (review replay-lineage-registry-20). Plain-looking values that
    YAML would read as another type (``true``, ``1.0``, ``null``) are quoted too.
    """
    if value and _YAML_PLAIN.fullmatch(value) and isinstance(yaml.safe_load(value), str):
        return value
    return json.dumps(value, ensure_ascii=False)


@dataclass
class RegistrationSuggestion:
    asset_type: str
    detected_name: str
    detected_version: str
    confidence: float
    call_count: int
    evidence: list[str]
    draft_spec_yaml: str
    warnings: list[str] = field(default_factory=list)


class SuggestionEngine:
    """Analyze capsule observational data and suggest unregistered assets."""

    def analyze(
        self, capsule_dir: Path, db_path: Path | None = None
    ) -> list[RegistrationSuggestion]:
        """Return suggestions for one capsule, filtered against the registry."""
        results: list[RegistrationSuggestion] = []
        results.extend(self._analyze_models(capsule_dir))
        results.extend(self._analyze_tools(capsule_dir))
        results.extend(self._analyze_agent(capsule_dir))
        if db_path is not None:
            results = [
                s for s in results
                if not self._is_registered(s.detected_name, s.asset_type, db_path)
            ]
        return results

    def analyze_recent(
        self,
        base_dir: Path,
        db_path: Path | None = None,
        limit: int = 10,
    ) -> list[RegistrationSuggestion]:
        """Aggregate suggestions across the most recent *limit* capsules."""
        if not base_dir.exists():
            return []
        # Never a linked entry, and no file below is read through a link.
        capsule_dirs = sorted(
            iter_capsule_dirs(base_dir),
            key=lambda d: d.lstat().st_mtime,
            reverse=True,
        )[:limit]

        merged: dict[tuple[str, str], RegistrationSuggestion] = {}
        for cdir in capsule_dirs:
            for s in self.analyze(cdir, db_path=None):
                key = (s.asset_type, s.detected_name)
                if key in merged:
                    prev = merged[key]
                    merged[key] = RegistrationSuggestion(
                        asset_type=s.asset_type,
                        detected_name=s.detected_name,
                        detected_version=prev.detected_version or s.detected_version,
                        confidence=max(prev.confidence, s.confidence),
                        call_count=prev.call_count + s.call_count,
                        evidence=list(dict.fromkeys(prev.evidence + s.evidence)),
                        draft_spec_yaml=prev.draft_spec_yaml,
                        warnings=list(dict.fromkeys(prev.warnings + s.warnings)),
                    )
                else:
                    merged[key] = s

        results = list(merged.values())
        if db_path is not None:
            results = [
                s for s in results
                if not self._is_registered(s.detected_name, s.asset_type, db_path)
            ]
        return sorted(results, key=lambda s: (-s.confidence, -s.call_count))

    # ------------------------------------------------------------------ private

    def _analyze_models(self, capsule_dir: Path) -> list[RegistrationSuggestion]:
        path = capsule_dir / "model-calls.jsonl"
        if not path.exists():
            return []

        counts: dict[tuple[str, str], int] = {}
        # ADR-0305: count logical calls, not the wire hook's transport copies.
        for r in read_logical_model_calls(path):
            system = r.get("gen_ai.system", "unknown")
            model_id = r.get("gen_ai.response.model") or r.get("gen_ai.request.model", "")
            if model_id:
                counts[(system, model_id)] = counts.get((system, model_id), 0) + 1

        out = []
        for (system, model_id), call_count in counts.items():
            name, version = _parse_model_identity(model_id)
            out.append(
                RegistrationSuggestion(
                    asset_type="model",
                    detected_name=name,
                    detected_version=version,
                    confidence=1.0,
                    call_count=call_count,
                    evidence=[str(path)],
                    draft_spec_yaml=_model_draft_yaml(name, version, system, model_id),
                )
            )
        return out

    def _analyze_tools(self, capsule_dir: Path) -> list[RegistrationSuggestion]:
        path = capsule_dir / "tool-calls.jsonl"
        if not path.exists():
            return []

        info: dict[str, dict[str, Any]] = {}
        for line in _read_stream(capsule_dir, "tool-calls.jsonl").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = r.get("tool_name", "")
            if not name:
                continue
            if name not in info:
                info[name] = {
                    "count": 0,
                    "transport": r.get("transport", "local"),
                    "provider": r.get("tool_provider", ""),
                }
            info[name]["count"] += 1

        out = []
        for tool_name, meta in info.items():
            if meta["count"] < 2:
                continue
            out.append(
                RegistrationSuggestion(
                    asset_type="tool",
                    detected_name=tool_name,
                    detected_version="0.1.0",
                    confidence=0.8,
                    call_count=meta["count"],
                    evidence=[str(path)],
                    draft_spec_yaml=_tool_draft_yaml(
                        tool_name, meta["transport"], meta["provider"]
                    ),
                )
            )
        return out

    def _analyze_agent(self, capsule_dir: Path) -> list[RegistrationSuggestion]:
        manifest_path = capsule_dir / "capsule.yaml"
        if not manifest_path.exists():
            return []
        try:
            import yaml

            manifest: dict[str, Any] = (
                yaml.safe_load(_read_stream(capsule_dir, "capsule.yaml")) or {}
            )
        except Exception:
            return []

        command: list[str] = manifest.get("command", [])
        if not command:
            return []

        name = _infer_agent_name(command)
        git_sha: str = manifest.get("git_commit_sha", "")
        top_model_info = self._top_model(capsule_dir)
        top_model = top_model_info[0] if top_model_info else None
        top_system = top_model_info[1] if top_model_info else None
        tool_names = self._all_tool_names(capsule_dir)

        return [
            RegistrationSuggestion(
                asset_type="agent",
                detected_name=name,
                detected_version="0.1.0",
                confidence=0.7,
                call_count=1,
                evidence=[str(manifest_path)],
                draft_spec_yaml=_agent_draft_yaml(
                    name, "0.1.0", command, git_sha, top_model, top_system, tool_names
                ),
                warnings=[
                    "spec.evals is required — add at least one eval suite before registering"
                ],
            )
        ]

    def _top_model(self, capsule_dir: Path) -> tuple[str, str] | None:
        """Return (model_id, system) for the most-called model, or None."""
        path = capsule_dir / "model-calls.jsonl"
        if not path.exists():
            return None
        counts: dict[tuple[str, str], int] = {}
        for r in read_logical_model_calls(path):
            m = r.get("gen_ai.request.model", "")
            s = r.get("gen_ai.system", "unknown")
            if m:
                counts[(m, s)] = counts.get((m, s), 0) + 1
        if not counts:
            return None
        best = max(counts, key=lambda k: counts[k])
        return best

    def _all_tool_names(self, capsule_dir: Path) -> list[str]:
        path = capsule_dir / "tool-calls.jsonl"
        if not path.exists():
            return []
        names: set[str] = set()
        for line in _read_stream(capsule_dir, "tool-calls.jsonl").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                n = r.get("tool_name", "")
                if n:
                    names.add(n)
            except json.JSONDecodeError:
                pass
        return sorted(names)

    def _is_registered(self, name: str, asset_type: str, db_path: Path) -> bool:
        try:
            from novafabric.registry.service import list_assets

            assets = list_assets(asset_type=asset_type, status=None, db_path=db_path)
            return any(a["name"] == name for a in assets)
        except Exception:
            return False


# ------------------------------------------------------------------ helpers


def _parse_model_identity(model_id: str) -> tuple[str, str]:
    """Return (name, semver_version) from a raw model identifier string."""
    # Handle tag syntax: qwen3.6:35b → qwen3.6-35b
    clean = model_id.replace(":", "-").replace("/", "-")

    # Try to extract YYYY-MM-DD suffix: gpt-4o-2024-08-06 → (gpt-4o, 2024.8.6)
    date_match = re.search(r"-(\d{4})-(\d{2})-(\d{2})$", model_id)
    if date_match:
        y, m, d = date_match.groups()
        base = model_id[: date_match.start()].replace(":", "-").replace("/", "-")
        return base, f"{y}.{int(m)}.{int(d)}"

    # Try YYYYMMDD suffix: claude-3-5-sonnet-20241022 → (claude-3-5-sonnet, 2024.10.22)
    yyyymmdd_match = re.search(r"-(\d{4})(\d{2})(\d{2})$", model_id)
    if yyyymmdd_match:
        y, m, d = yyyymmdd_match.groups()
        base = model_id[: yyyymmdd_match.start()].replace(":", "-").replace("/", "-")
        return base, f"{y}.{int(m)}.{int(d)}"

    return clean, "0.1.0"


def _model_draft_yaml(name: str, version: str, system: str, model_id: str) -> str:
    return (
        f'novafabric_spec_version: "1"\n'
        f"asset_type: model\n"
        f"name: {_yaml_str(name)}\n"
        f"version: {_yaml_str(version)}\n"
        f"status: development\n"
        f"description: \"\"\n"
        f"spec:\n"
        f"  framework: {_yaml_str(system)}\n"
        f"  artifact_path: {_yaml_str(model_id)}\n"
    )


def _tool_draft_yaml(tool_name: str, transport: str, provider: str) -> str:
    entrypoint = provider if provider else f"{transport}://{tool_name}"
    return (
        f'novafabric_spec_version: "1"\n'
        f"asset_type: tool\n"
        f"name: {_yaml_str(tool_name)}\n"
        f"version: 0.1.0\n"
        f"status: development\n"
        f"description: \"\"\n"
        f"spec:\n"
        f"  entrypoint: {json.dumps(entrypoint, ensure_ascii=False)}\n"
    )


def _infer_agent_name(command: list[str]) -> str:
    for part in command:
        if part.endswith(".py"):
            return Path(part).stem
    for part in reversed(command):
        if part not in ("python", "python3", "uv", "run") and not part.startswith("-"):
            return part
    return "my-agent"


def _agent_draft_yaml(
    name: str,
    version: str,
    command: list[str],
    git_sha: str,
    top_model: str | None,
    top_system: str | None,
    tool_names: list[str],
) -> str:
    if top_model:
        sys_hint = top_system or ""
        if any(k in top_model for k in ("gpt", "openai")) or "openai" in sys_hint:
            provider = "openai"
        elif any(k in top_model for k in ("claude", "anthropic")) or "anthropic" in sys_hint:
            provider = "anthropic"
        elif "ollama" in sys_hint or "ollama" in top_model:
            provider = "ollama"
        else:
            provider = "unknown"
        model_block = (
            f"  model:\n    provider: {provider}\n    name: {_yaml_str(top_model)}\n"
        )
    else:
        model_block = "  model:\n    provider: unknown\n    name: unknown\n"

    tools_block = (
        "  tools:\n" + "".join(f"    - {_yaml_str(t)}\n" for t in tool_names)
        if tool_names
        else "  tools: []\n"
    )

    return (
        f'novafabric_spec_version: "1"\n'
        f"asset_type: agent\n"
        f"name: {_yaml_str(name)}\n"
        f"version: {_yaml_str(version)}\n"
        f"status: development\n"
        f"description: \"\"\n"
        f"spec:\n"
        f"{model_block}"
        f"{tools_block}"
        f"  prompts:\n"
        f'    system: ""\n'
        f"  policies: []\n"
        f"  evals:\n"
        f"    - REQUIRED  # replace with a real eval suite name\n"
    )
