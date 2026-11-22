"""The minimal, diffable Run Capsule a re-executing replay writes (ADR-0321).

``intervention`` (ADR-0086) and ``mocked`` replays write one into the replay
result directory, hard-marked with ``replay_mode`` and ``replay_of_run_id`` so it
is never mistaken for an original run, so that ``nova diff <original> <replay>``
compares the two runs.

A mocked replay's streams are what the replay *did*: the recorded records the
dispatchers served (and the records a served nested ``record.tool`` boundary
covered), in the order the replay reached them, plus one placeholder record for
each call that was not served from the capsule (``io.novafabric.replay_placeholder``:
``live``, ``refused`` or ``unmatched``). The replayed process's own requests are
not recorded -- a placeholder carries no request or response, timing or span:
only what the replay observed, valid under the ``tool-call`` / ``model-call``
schemas' placeholder branch. Its
``outputs/stdout.txt`` / ``outputs/stderr.txt`` are the replayed streams, written
only when not empty (as ``nova capture`` writes them) and passed through the same
built-in secret scanner capture uses, whose proof is ``redaction-proof.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from novafabric.capsule.ulid_util import new_ulid
from novafabric.capture._tool_codec import MUTATION_CLASSES
from novafabric.capture.record_roles import RECORD_ROLE_EXT, ROLE_LOGICAL
from novafabric.replay._contract import (
    MODEL_SURFACES,
    TOOL_SURFACE_MCP,
    interceptable_tool_calls,
    model_queues,
)
from novafabric.replay._tool_call_outcomes import collapse_tool_divergences

logger = logging.getLogger(__name__)

#: Extension key on a record the replay capsule holds for a call that was not
#: served from the capsule.
PLACEHOLDER_EXT = "io.novafabric.replay_placeholder"
#: On a placeholder: the intercepted surface the call was made on.
REPLAY_SURFACE_EXT = "io.novafabric.replay_surface"
#: ``schema_version`` of the call records (``tool-call`` / ``model-call`` 0.1.0).
RECORD_SCHEMA_VERSION = "0.1.0"

REPLAY_CAPSULE_FILE = "capsule.yaml"


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)


def _write_streams(
    result_dir: Path, model_calls: list[dict[str, Any]], tool_calls: list[dict[str, Any]]
) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "model-calls.jsonl").write_text(_jsonl(model_calls))
    (result_dir / "tool-calls.jsonl").write_text(_jsonl(tool_calls))


def _write_manifest(result_dir: Path, manifest: dict[str, Any]) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    path = result_dir / REPLAY_CAPSULE_FILE
    path.write_text(yaml.dump(manifest, allow_unicode=True))
    return path


def write_capsule_streams(
    result_dir: Path,
    manifest: dict[str, Any],
    model_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> Path:
    """Write ``capsule.yaml`` and the two call streams; return the manifest path."""
    path = _write_manifest(result_dir, manifest)
    _write_streams(result_dir, model_calls, tool_calls)
    return path


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _model_placeholder(ev: dict[str, Any]) -> dict[str, Any]:
    """A model call that ran live. Only what the replay observed: which provider
    and surface -- never a request, a response, a timing or a status (the replay
    did not see how it ended). Valid under the schemas' placeholder branch."""
    extensions: dict[str, Any] = {RECORD_ROLE_EXT: ROLE_LOGICAL, PLACEHOLDER_EXT: "live"}
    surface = _text(ev.get("surface"))
    if surface is not None:
        extensions[REPLAY_SURFACE_EXT] = surface
    record: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "model_call_id": new_ulid(),
    }
    # The registry system when the seam knows it (generic wire), else the provider.
    provider = _text(ev.get("system")) or _text(ev.get("provider"))
    if provider is not None:
        record["gen_ai.system"] = provider
    record["extensions"] = extensions
    return record


def _tool_placeholder(ev: dict[str, Any], kind: str) -> dict[str, Any]:
    """A tool call the replay did not serve (``kind``: ``live``, ``refused`` or
    ``unmatched``). ``status`` is ``denied`` when the replay stopped it before it
    ran, and absent when it ran live (its outcome was not observed)."""
    record: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "tool_call_id": new_ulid(),
        "tool_name": _text(ev.get("tool_name")) or "unknown",
    }
    mutation_class = ev.get("mutation_class")
    if mutation_class in MUTATION_CLASSES:
        record["mutation_class"] = mutation_class
    if kind != "live":
        record["status"] = "denied"
    record["extensions"] = {
        PLACEHOLDER_EXT: kind,
        REPLAY_SURFACE_EXT: _text(ev.get("surface")) or TOOL_SURFACE_MCP,
    }
    return record


class _ToolRecords:
    """Recorded intercepted tool records, each handed out at most once."""

    def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
        self._records = interceptable_tool_calls(tool_calls)
        self._used: set[int] = set()

    def take(self, record_id: Any, name: Any) -> dict[str, Any] | None:
        for key, want in (("tool_call_id", record_id), ("tool_name", name)):
            if want is None:
                continue
            for idx, rec in enumerate(self._records):
                if idx not in self._used and rec.get(key) == want:
                    self._used.add(idx)
                    return rec
        return None


def replayed_streams(
    model_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The model and tool records of what a mocked replay did, in event order."""
    queues = model_queues(model_calls)
    tools = _ToolRecords(tool_calls)
    models_out: list[dict[str, Any]] = []
    tools_out: list[dict[str, Any]] = []
    # A divergence then a live run (or refusal) of the same call is one record.
    for ev in collapse_tool_divergences(events):
        kind = ev.get("event")
        if kind in ("model_served", "model_covered"):
            queue = str(ev.get("queue") or ev.get("provider"))
            index = int(ev.get("call_index") or 0)
            records = queues.get(queue) or []
            if queue in MODEL_SURFACES and 0 <= index < len(records):
                models_out.append(records[index])
        elif kind == "model_live":
            models_out.append(_model_placeholder(ev))
        elif kind in ("tool_mocked", "tool_covered"):
            record = tools.take(ev.get("record_id"), ev.get("tool_name"))
            if record is not None:
                tools_out.append(record)
        elif kind in ("tool_live", "tool_refused"):
            tools_out.append(_tool_placeholder(ev, kind.removeprefix("tool_")))
        elif kind == "divergence" and ev.get("kind") in (
            "tool_call_unmatched", "tool_result_not_servable",
        ):
            tools_out.append(_tool_placeholder(ev, "unmatched"))
    return models_out, tools_out


def _environment(created_at: str, replay_id: str) -> dict[str, Any] | None:
    from novafabric.capture.env import capture_environment

    try:
        return capture_environment(created_at=created_at, run_id=replay_id)
    except Exception:  # noqa: BLE001 -- fail-open: the replay result stands without it
        logger.warning("novafabric: replay capsule env.lock could not be captured", exc_info=True)
        return None


def write_mocked_replay_capsule(
    *,
    result_dir: Path,
    replay_id: str,
    source_run_id: str,
    source_manifest: dict[str, Any],
    model_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    events: list[dict[str, Any]],
    exit_code: int | None,
    status: str,
    stdout: bytes | None,
    stderr: bytes | None,
    created_at: str,
    finished_at: str,
) -> bool:
    """Write the mocked replay's capsule into *result_dir*.

    Returns whether the output streams were stored after a successful secret
    scan (``False``: the fidelity check falls back to the raw digests).
    """
    from novafabric.capture.capsule import CapsuleWriter
    from novafabric.capture.finalize import finalize_in_process_capsule

    # The same finalization as every in-process capsule (main secret scan, manifest
    # redaction, lineage, residual pass, evidence digests, manifest gate, opt-in seal):
    # a replay capsule holds replayed output, so it gets no weaker treatment.
    writer = CapsuleWriter(run_id=result_dir.name, base_dir=result_dir.parent)
    writer.open()
    for name, data in (("stdout.txt", stdout), ("stderr.txt", stderr)):
        if data:
            writer.write_bytes(f"outputs/{name}", data)
    served_models, served_tools = replayed_streams(model_calls, tool_calls, events)
    writer.write_text("model-calls.jsonl", _jsonl(served_models))
    writer.write_text("tool-calls.jsonl", _jsonl(served_tools))
    env_lock = _environment(created_at, replay_id)
    if env_lock is not None:
        writer.write_text("env.lock", yaml.dump(env_lock, allow_unicode=True))
    manifest: dict[str, Any] = {
        "run_id": replay_id,
        "replay_mode": "mocked",
        "replay_of_run_id": source_run_id,
        "created_at": created_at,
        "finished_at": finished_at,
        "status": status,
        "command": source_manifest.get("command", []),
        "environment_ref": "env.lock" if env_lock is not None else None,
        "model_calls_ref": "model-calls.jsonl",
        "tool_calls_ref": "tool-calls.jsonl",
        "redaction_proof_ref": "redaction-proof.json",
        "replay_result_ref": "replay_result.yaml",
        "model_call_count": len(served_models),
        "tool_call_count": len(served_tools),
        "exit_code": exit_code,
    }
    finalize_in_process_capsule(result_dir, manifest, run_id=replay_id, writer=writer)
    scanned = (result_dir / "redaction-proof.json").is_file()
    if not scanned:  # never leave replayed output on disk unscanned
        logger.warning("novafabric: replay capsule secret scan failed; output streams removed")
        for name in ("stdout.txt", "stderr.txt"):
            (result_dir / "outputs" / name).unlink(missing_ok=True)
    return scanned
