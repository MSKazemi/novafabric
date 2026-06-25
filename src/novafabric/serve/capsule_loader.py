"""Walks `.novafabric/runs/` and parses capsule.yaml files.

The CLI never centralised this logic — each command did its own parsing.
The dashboard needs it as a single, typed entry point.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def discover_capsule_dirs(base: Path) -> list[Path]:
    """Return every directory under `base` that contains a capsule.yaml."""
    if not base.exists() or not base.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(base.iterdir()):
        if entry.is_dir() and (entry / "capsule.yaml").exists():
            out.append(entry)
    return out


# Event files KG ingest reads; a dir is "ingestable" if it has any of them.
_KG_EVENT_FILES = ("model-calls.jsonl", "tool-calls.jsonl", "events.jsonl")


def discover_ingestable_dirs(base: Path) -> list[Path]:
    """Return subdirs of *base* containing a KG-ingestable event file.

    Unlike :func:`discover_capsule_dirs` (which requires capsule.yaml), this
    finds any directory holding ``model-calls.jsonl`` / ``tool-calls.jsonl`` /
    ``events.jsonl`` — the files KG ingest actually reads. Real capsules (which
    always carry a manifest) are a subset of what this returns, so KG bulk
    ingest no longer skips event-only capture directories.
    """
    if not base.exists() or not base.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(base.iterdir()):
        if entry.is_dir() and any((entry / f).exists() for f in _KG_EVENT_FILES):
            out.append(entry)
    return out


def load_capsule_manifest(capsule_dir: Path) -> dict[str, Any]:
    """Parse capsule.yaml and return its dict. Raises FileNotFoundError if missing."""
    manifest_path = capsule_dir / "capsule.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"capsule.yaml not found in {capsule_dir}")
    with manifest_path.open("r") as f:
        return yaml.safe_load(f) or {}


def load_jsonl(capsule_dir: Path, filename: str) -> list[dict[str, Any]]:
    """Parse a JSONL file relative to the capsule directory; empty list if missing."""
    p = capsule_dir / filename
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def list_run_summaries(base: Path) -> list[dict[str, Any]]:
    """Compact one-line-per-run summary for the dashboard list view."""
    summaries: list[dict[str, Any]] = []
    for d in discover_capsule_dirs(base):
        try:
            m = load_capsule_manifest(d)
        except (FileNotFoundError, yaml.YAMLError):
            continue
        summaries.append({
            "run_id": m.get("run_id", d.name),
            "status": m.get("status"),
            "created_at": m.get("created_at"),
            "finished_at": m.get("finished_at"),
            "duration_ms": m.get("duration_ms"),
            "exit_code": m.get("exit_code"),
            "model_call_count": m.get("model_call_count", 0),
            "tool_call_count": m.get("tool_call_count", 0),
            "mutating_tool_count": m.get("mutating_tool_count", 0),
            "command": m.get("command", []),
            "novafabric_version": m.get("novafabric_version"),
            "capsule_path": str(d.resolve()),
        })
    # Newest first
    summaries.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return summaries


def _list_dir_files(capsule_dir: Path, subdir: str) -> list[dict[str, Any]]:
    """Return [{name, size_bytes}] for files directly inside a capsule subdirectory."""
    d = capsule_dir / subdir
    if not d.is_dir():
        return []
    return sorted(
        [{"name": f.name, "size_bytes": f.stat().st_size} for f in d.iterdir() if f.is_file()],
        key=lambda x: x["name"],
    )


def load_full_capsule(capsule_dir: Path) -> dict[str, Any]:
    """Manifest + every parsed sub-file. Used by /api/runs/{run_id}."""
    manifest = load_capsule_manifest(capsule_dir)
    return {
        "run_id": manifest.get("run_id", capsule_dir.name),
        "capsule_path": str(capsule_dir.resolve()),
        "manifest": manifest,
        "trace": load_jsonl(capsule_dir, "trace.jsonl"),
        "model_calls": load_jsonl(capsule_dir, "model-calls.jsonl"),
        "tool_calls": load_jsonl(capsule_dir, "tool-calls.jsonl"),
        "lineage": load_jsonl(capsule_dir, "lineage.jsonl"),
        "assets": load_jsonl(capsule_dir, "assets.jsonl"),
        "inputs": _list_dir_files(capsule_dir, "inputs"),
        "outputs": _list_dir_files(capsule_dir, "outputs"),
    }
