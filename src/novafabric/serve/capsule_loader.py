"""Walks `.novafabric/runs/` and parses capsule.yaml files.

The CLI never centralised this logic — each command did its own parsing.
The dashboard needs it as a single, typed entry point.

Every capsule file is read through :mod:`novafabric._capsule_fs`, so a link
planted in a capsule (``capsule.yaml -> ~/.kube/config``) is never followed: the
manifest reads as missing, a JSONL stream reads as empty (logged), and the
``inputs``/``outputs`` listings leave links out.
"""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path
from typing import Any

import yaml

from novafabric._capsule_fs import CapsuleEntryError, iter_capsule_dirs, read_capsule_file
from novafabric.capture.record_roles import split_model_calls

_log = logging.getLogger(__name__)
_OPERATION = "the dashboard"


def discover_capsule_dirs(base: Path) -> list[Path]:
    """Return every directory under `base` that contains a capsule.yaml.

    Delegates to :func:`novafabric._capsule_fs.iter_capsule_dirs`: an entry
    that is itself a link (to a directory inside *or* outside `base`) is never
    listed, and neither is one whose ``capsule.yaml`` is a link.
    """
    if not base.exists() or not base.is_dir():
        return []
    return iter_capsule_dirs(base)


def confined_capsule_root(candidate: Path, capsule_dir: Path) -> Path | None:
    """*candidate* (an entry of *capsule_dir*), or ``None`` if ``nova serve`` must refuse it.

    A real directory is returned as is. A link is returned *resolved* when its
    target lies inside the server's read roots (the capsule dir itself, so an
    alias such as ``latest -> 01J...`` works, ``$NOVAFABRIC_HOME`` minus its
    secrets, and any ``NOVAFABRIC_SERVE_PATH_ROOTS`` entry), and is ``None``
    otherwise: a link to a directory elsewhere would put that directory behind
    every ``{run_id}`` route. Returning the resolved path means a link swapped
    after this check is not followed by the read.
    """
    if not candidate.is_symlink():
        return candidate
    from novafabric.serve import paths as serve_paths  # noqa: PLC0415

    try:
        return serve_paths.confine_read(candidate, capsule_dir)
    except serve_paths.PathConfinementError as exc:
        _log.warning(
            "capsule_root_link_refused",
            extra={"capsule": str(candidate), "reason": str(exc)[:200]},
        )
        return None


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
    return iter_capsule_dirs(base, markers=_KG_EVENT_FILES)


def load_capsule_manifest(capsule_dir: Path) -> dict[str, Any]:
    """Parse capsule.yaml and return its dict. Raises FileNotFoundError if missing."""
    try:
        raw = read_capsule_file(capsule_dir, "capsule.yaml", operation=_OPERATION)
    except CapsuleEntryError as exc:
        # A linked or special manifest is unreadable, never followed.
        raise FileNotFoundError(
            f"capsule.yaml in {capsule_dir} is not a regular file: {exc}"
        ) from exc
    except FileNotFoundError:
        raise FileNotFoundError(f"capsule.yaml not found in {capsule_dir}") from None
    return yaml.safe_load(raw.decode("utf-8")) or {}


def load_jsonl(capsule_dir: Path, filename: str) -> list[dict[str, Any]]:
    """Parse a JSONL file relative to the capsule directory; empty list if missing."""
    try:
        raw = read_capsule_file(capsule_dir, filename, operation=_OPERATION)
    except FileNotFoundError:
        return []
    except CapsuleEntryError as exc:
        _log.warning(
            "capsule_file_not_followed",
            extra={"capsule": str(capsule_dir), "file": filename, "reason": str(exc)},
        )
        return []
    out: list[dict[str, Any]] = []
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def summarize_manifest(capsule_dir: Path, m: dict[str, Any]) -> dict[str, Any]:
    """The one-line run summary the dashboard list renders, from one manifest."""
    return {
        "run_id": m.get("run_id", capsule_dir.name),
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
        "capsule_path": str(capsule_dir.resolve()),
    }


def list_run_summaries(base: Path) -> list[dict[str, Any]]:
    """Compact one-line-per-run summary for the dashboard list view."""
    summaries: list[dict[str, Any]] = []
    for d in discover_capsule_dirs(base):
        try:
            m = load_capsule_manifest(d)
        except (FileNotFoundError, yaml.YAMLError):
            continue
        summaries.append(summarize_manifest(d, m))
    # Newest first
    summaries.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return summaries


def run_summaries_for(base: Path, run_ids: list[str]) -> list[dict[str, Any]]:
    """Summaries for exactly *run_ids*, in the order given, in one directory pass.

    A run id with no readable capsule is omitted rather than invented — the
    caller reports how many it asked for, so a gap is visible, not papered over.
    """
    wanted = set(run_ids)
    found: dict[str, dict[str, Any]] = {}
    for d in discover_capsule_dirs(base):
        if len(found) == len(wanted):
            break
        try:
            m = load_capsule_manifest(d)
        except (FileNotFoundError, yaml.YAMLError):
            continue
        rid = str(m.get("run_id") or d.name)
        if rid in wanted and rid not in found:
            found[rid] = summarize_manifest(d, m)
    return [found[r] for r in run_ids if r in found]


def _list_dir_files(capsule_dir: Path, subdir: str) -> list[dict[str, Any]]:
    """Return [{name, size_bytes}] for files directly inside a capsule subdirectory."""
    d = capsule_dir / subdir
    if d.is_symlink() or not d.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for f in d.iterdir():
        st = f.lstat()  # a link is left out: its target's size is not capsule data
        if stat.S_ISREG(st.st_mode):
            files.append({"name": f.name, "size_bytes": st.st_size})
    return sorted(files, key=lambda x: x["name"])


def load_full_capsule(capsule_dir: Path) -> dict[str, Any]:
    """Manifest + every parsed sub-file. Used by /api/runs/{run_id}."""
    manifest = load_capsule_manifest(capsule_dir)
    # ADR-0305: ``model_calls`` holds one record per logical call; the wire
    # hook's transport records (HTTP attempts under an SDK call) are kept,
    # additively, under ``transport_model_calls``.
    model_calls, transport = split_model_calls(load_jsonl(capsule_dir, "model-calls.jsonl"))
    return {
        "run_id": manifest.get("run_id", capsule_dir.name),
        "capsule_path": str(capsule_dir.resolve()),
        "manifest": manifest,
        "trace": load_jsonl(capsule_dir, "trace.jsonl"),
        "model_calls": model_calls,
        "transport_model_calls": transport,
        "tool_calls": load_jsonl(capsule_dir, "tool-calls.jsonl"),
        "lineage": load_jsonl(capsule_dir, "lineage.jsonl"),
        "assets": load_jsonl(capsule_dir, "assets.jsonl"),
        "inputs": _list_dir_files(capsule_dir, "inputs"),
        "outputs": _list_dir_files(capsule_dir, "outputs"),
    }
