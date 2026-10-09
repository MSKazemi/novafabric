from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from novafabric.capture.record_roles import logical_model_calls
from novafabric.diff._align import align_model_calls, align_tool_calls
from novafabric.diff._report import DiffReport


class CapsuleFileError(ValueError):
    """A capsule's ``capsule.yaml`` or ``env.lock`` exists but cannot be read as one.

    Not readable, not UTF-8, not YAML, or YAML whose top level is not a mapping
    (or, for ``env.lock``, whose compared ``python``/``host`` section is not a
    mapping). The two capsules then cannot be compared: ``nova diff`` exits 2
    in every output format, gate flag or not, and ``GET /api/diff`` answers 422
    (ADR-0303 Amendment 2). Before, the parse error escaped as a traceback,
    which Python exits with 1 — the code reserved for "found a difference".
    A *missing* or empty file is not this error: older capsules lack fields,
    and the diff reads what is there.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Records of a capsule JSONL file, and how many non-blank lines were skipped.

    A line is skipped when it is not UTF-8, not JSON, or JSON that is not an
    object. It used to be dropped silently, so a corrupted record vanished from
    both sides of the comparison and ``--assert-no-regressions`` passed on runs
    it had not fully read; a valid-JSON non-object line (``[1, 2]``) crashed the
    aligner instead, exiting 1 as if a difference had been found. The count is
    reported per side (``DiffReport.skipped_malformed_lines``) and makes the
    gate exit 2, "cannot compare" (ADR-0303 Amendment 1).

    Lines are split on bytes, so only ``\\n``/``\\r`` end a record: JSON may
    carry U+2028 unescaped inside a string, and ``str.splitlines`` cut there.
    """
    records: list[dict[str, Any]] = []
    skipped = 0
    if not path.exists():
        return records, skipped
    for raw in path.read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            skipped += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            skipped += 1
    return records, skipped


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
    """A capsule YAML file as a mapping; ``{}`` when it is absent or empty.

    Raises:
        CapsuleFileError: the file exists but cannot be read, is not UTF-8, is
            not YAML, or its top level is not a mapping.
    """
    if not path.exists():
        return {}
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise CapsuleFileError(path, f"cannot be read ({exc.strerror or exc})") from exc
    except UnicodeDecodeError as exc:
        raise CapsuleFileError(path, f"not UTF-8 (byte {exc.start})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise CapsuleFileError(path, f"not valid YAML{where}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CapsuleFileError(path, f"top level is a {type(data).__name__}, not a mapping")
    return data


#: ``env.lock`` fields the environment section compares, as (section, key).
_ENV_FIELDS = (
    ("python", "version"),
    ("python", "interpreter"),
    ("host", "os"),
    ("host", "arch"),
)


def _env_fields(env: dict[str, Any], path: Path) -> dict[str, Any]:
    """The compared ``env.lock`` fields; a present non-mapping section is malformed."""
    fields: dict[str, Any] = {}
    for section, key in _ENV_FIELDS:
        block = env.get(section)
        if block is None:
            block = {}
        if not isinstance(block, dict):
            raise CapsuleFileError(
                path, f"'{section}' is a {type(block).__name__}, not a mapping"
            )
        fields[f"{section}.{key}"] = block.get(key)
    return fields


#: Prefix of the OTel GenAI request attributes capture records on a model call.
_REQUEST_PREFIX = "gen_ai.request."
#: Request attributes compared on their own (``request_changed``), not as parameters.
_REQUEST_CORE = frozenset({"gen_ai.request.model", "gen_ai.request.messages"})


def _changed_params(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Recorded request parameters that differ between two paired calls, sorted.

    Every ``gen_ai.request.*`` attribute other than the model and messages —
    temperature, top_p, top_k, max_tokens, seed, stop_sequences, the penalties,
    choice.count, and any added later. A key recorded on one side only is a
    change: capture records a parameter only when the request set it, so its
    absence is not a default value (ADR-0303 Amendment 2).
    """
    keys = {
        k
        for k in a.keys() | b.keys()
        if k.startswith(_REQUEST_PREFIX) and k not in _REQUEST_CORE
    }
    return sorted(k for k in keys if (k in a) != (k in b) or a.get(k) != b.get(k))


class DiffEngine:
    def compare(self, capsule_a: Path, capsule_b: Path) -> DiffReport:
        """Structural diff of two capsules.

        Raises:
            CapsuleFileError: a ``capsule.yaml`` or ``env.lock`` exists but is
                unreadable or malformed, so the capsules cannot be compared.
        """
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
        # Equal documents establish "no environment change" whatever their shape.
        if env_a == env_b:
            return
        # Compare key fields only (avoid package list noise)
        fields_a = _env_fields(env_a, capsule_a / "env.lock")
        fields_b = _env_fields(env_b, capsule_b / "env.lock")
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
        calls_a, report.skipped_malformed_lines["a"]["model_calls"] = _read_jsonl(
            capsule_a / "model-calls.jsonl"
        )
        calls_b, report.skipped_malformed_lines["b"]["model_calls"] = _read_jsonl(
            capsule_b / "model-calls.jsonl"
        )
        # ADR-0305: align logical calls only. The wire hook's transport records
        # (one per HTTP attempt under an SDK call) are not calls: pairing them
        # too reported one changed prompt as two changed pairs.
        pairs = align_model_calls(logical_model_calls(calls_a), logical_model_calls(calls_b))

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
                # Compare request (provider, model, messages) and response (choices).
                # The provider is part of the request: the alignment's request key
                # (_align._request_key) includes it, and a call that moved to another
                # provider under the same model name and prompt was reported as
                # unchanged, so --assert-no-regressions passed on it.
                provider_changed = a.get("gen_ai.system") != b.get("gen_ai.system")
                # Sampling parameters are part of the request too: a temperature
                # or seed change can alter every response (ADR-0303 Am. 2).
                changed_params = _changed_params(a, b)
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
                req_changed = req_a != req_b or provider_changed or bool(changed_params)
                resp_changed = resp_a != resp_b
                changed = req_changed or resp_changed
                report.model_call_pairs.append({
                    "changed": changed,
                    "span_id": a.get("parent_span_id"),
                    "model_call_id_a": a.get("model_call_id"),
                    "model_call_id_b": b.get("model_call_id"),
                    "request_changed": req_changed,
                    "response_changed": resp_changed,
                    "provider_changed": provider_changed,
                    "params_changed": bool(changed_params),
                    "changed_params": changed_params,
                })

    def _diff_tool_calls(
        self, capsule_a: Path, capsule_b: Path, report: DiffReport
    ) -> None:
        calls_a, report.skipped_malformed_lines["a"]["tool_calls"] = _read_jsonl(
            capsule_a / "tool-calls.jsonl"
        )
        calls_b, report.skipped_malformed_lines["b"]["tool_calls"] = _read_jsonl(
            capsule_b / "tool-calls.jsonl"
        )
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
