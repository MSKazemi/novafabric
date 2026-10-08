from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from novafabric.diff._align import align_model_calls, align_tool_calls
from novafabric.diff._report import DiffReport


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    # O_NOFOLLOW: a file swapped for a symlink after the walk is refused, not
    # followed out of the capsule. Streamed, because an output can be large.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as fh:
        return "sha256:" + hashlib.file_digest(fh, "sha256").hexdigest()


def _output_files(capsule: Path) -> dict[str, Path]:
    """Every regular file under ``outputs/``, keyed by capsule-relative POSIX path.

    Walks with the same rules as the ADR-0251 evidence digests
    (``capture/orchestrator.py:_evidence_digests``, re-checked by ``nova verify``):
    recursive, regular files only, and a symlink is never followed. A symlinked
    file is skipped and a symlinked directory is not descended (``Path.rglob``
    does not recurse into one), so nothing outside the capsule is read — a
    workload that leaves ``outputs/x -> /etc`` cannot make the diff hash ``/etc``.
    An ``outputs`` that is itself a symlink contributes nothing, as it contributes
    nothing to the evidence digests. ``tests/test_diff_alignment_corpus.py`` pins
    the walked set against ``_evidence_digests`` so the two cannot drift.
    """
    root = capsule / "outputs"
    if root.is_symlink() or not root.is_dir():
        return {}
    return {
        path.relative_to(capsule).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


class DiffEngine:
    def compare(self, capsule_a: Path, capsule_b: Path) -> DiffReport:
        manifest_a = _load_yaml(capsule_a / "capsule.yaml")
        manifest_b = _load_yaml(capsule_b / "capsule.yaml")
        run_a_id = manifest_a.get("run_id", str(capsule_a))
        run_b_id = manifest_b.get("run_id", str(capsule_b))

        report = DiffReport(run_a_id=run_a_id, run_b_id=run_b_id)

        self._diff_env(capsule_a, capsule_b, report)
        self._diff_model_calls(capsule_a, capsule_b, report)
        self._diff_tool_calls(capsule_a, capsule_b, report)
        self._diff_outputs(capsule_a, capsule_b, report)

        return report

    def _diff_env(self, capsule_a: Path, capsule_b: Path, report: DiffReport) -> None:
        env_a = _load_yaml(capsule_a / "env.lock")
        env_b = _load_yaml(capsule_b / "env.lock")
        if env_a == env_b:
            return
        # Compare key fields only (avoid package list noise)
        fields_a = {
            "python.version": env_a.get("python", {}).get("version"),
            "python.interpreter": env_a.get("python", {}).get("interpreter"),
            "host.os": env_a.get("host", {}).get("os"),
            "host.arch": env_a.get("host", {}).get("arch"),
        }
        fields_b = {
            "python.version": env_b.get("python", {}).get("version"),
            "python.interpreter": env_b.get("python", {}).get("interpreter"),
            "host.os": env_b.get("host", {}).get("os"),
            "host.arch": env_b.get("host", {}).get("arch"),
        }
        for field, val_a in fields_a.items():
            val_b = fields_b.get(field)
            if val_a != val_b:
                report.env_changes.append({
                    "field": field,
                    "before": val_a,
                    "after": val_b,
                    "severity": "major" if "os" in field or "interpreter" in field else "minor",
                })

    def _diff_model_calls(
        self, capsule_a: Path, capsule_b: Path, report: DiffReport
    ) -> None:
        calls_a = _read_jsonl(capsule_a / "model-calls.jsonl")
        calls_b = _read_jsonl(capsule_b / "model-calls.jsonl")
        pairs = align_model_calls(calls_a, calls_b)

        for a, b in pairs:
            if a is None and b is not None:
                report.model_call_pairs.append({
                    "added": True,
                    "span_id": b.get("parent_span_id"),
                    "model_call_id_b": b.get("model_call_id"),
                })
            elif b is None and a is not None:
                report.model_call_pairs.append({
                    "removed": True,
                    "span_id": a.get("parent_span_id"),
                    "model_call_id_a": a.get("model_call_id"),
                })
            elif a is not None and b is not None:
                # Compare request (messages) and response (choices)
                req_a = {
                    "model": a.get("gen_ai.request.model"),
                    "messages": a.get("gen_ai.request.messages"),
                }
                req_b = {
                    "model": b.get("gen_ai.request.model"),
                    "messages": b.get("gen_ai.request.messages"),
                }
                resp_a = a.get("gen_ai.response.choices", [])
                resp_b = b.get("gen_ai.response.choices", [])
                req_changed = req_a != req_b
                resp_changed = resp_a != resp_b
                changed = req_changed or resp_changed
                report.model_call_pairs.append({
                    "changed": changed,
                    "span_id": a.get("parent_span_id"),
                    "model_call_id_a": a.get("model_call_id"),
                    "model_call_id_b": b.get("model_call_id"),
                    "request_changed": req_changed,
                    "response_changed": resp_changed,
                })

    def _diff_tool_calls(
        self, capsule_a: Path, capsule_b: Path, report: DiffReport
    ) -> None:
        calls_a = _read_jsonl(capsule_a / "tool-calls.jsonl")
        calls_b = _read_jsonl(capsule_b / "tool-calls.jsonl")
        pairs = align_tool_calls(calls_a, calls_b)

        for a, b in pairs:
            if a is None and b is not None:
                report.tool_call_pairs.append({
                    "added": True,
                    "tool_name": b.get("tool_name"),
                    "tool_call_id_b": b.get("tool_call_id"),
                })
            elif b is None and a is not None:
                report.tool_call_pairs.append({
                    "removed": True,
                    "tool_name": a.get("tool_name"),
                    "tool_call_id_a": a.get("tool_call_id"),
                })
            elif a is not None and b is not None:
                result_changed = a.get("result") != b.get("result")
                # Positional pairing (diff-report-v1) can pair a call whose
                # arguments changed; that is a change too, not only the result.
                arguments_changed = a.get("arguments") != b.get("arguments")
                report.tool_call_pairs.append({
                    "changed": result_changed or arguments_changed,
                    "tool_name": a.get("tool_name"),
                    "tool_call_id_a": a.get("tool_call_id"),
                    "tool_call_id_b": b.get("tool_call_id"),
                    "result_changed": result_changed,
                    "arguments_changed": arguments_changed,
                })

    def _diff_outputs(
        self, capsule_a: Path, capsule_b: Path, report: DiffReport
    ) -> None:
        files_a = _output_files(capsule_a)
        files_b = _output_files(capsule_b)
        for rel in sorted(files_a.keys() | files_b.keys()):
            h_a = _file_hash(files_a[rel]) if rel in files_a else ""
            h_b = _file_hash(files_b[rel]) if rel in files_b else ""
            if h_a != h_b:
                report.output_changes.append({
                    "path": rel,
                    "before_hash": h_a or None,
                    "after_hash": h_b or None,
                })
