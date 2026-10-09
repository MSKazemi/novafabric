"""Read-only collector for the NF-278 credit-decision evidence pack (ADR-0159 D6).

Reads one sealed Run Capsule directory and returns the typed
:class:`~.adverse_action.CapsuleFacts` that :func:`~.adverse_action.build_adverse_action_pack`
renders. Nothing is inferred, computed, or defaulted into existence:

* ``capsule.yaml`` — ``run_id``, ``status``, the ADR-0251 ``evidence_digests`` map, and the
  ``facets.feature_attribution`` block if present (the recorded principal reasons — not yet a
  registered run-capsule facet, ADR-0196 D2, so schema-valid capsules never carry it today);
* ``model-calls.jsonl`` — each record reduced to its model ref, status, and SHA-256 digests of the
  recorded request messages / response choices (the prompt and response text are never copied out);
  Every recorded string the pack renders (reason text, attribution ``method`` / ``producer`` /
  ``model_call_id``, model-call refs / status / timestamps) is length-capped and scanned against
  the ADR-0009 secret rules; a hit is suppressed (never rendered) and its row marked ``partial``.
  ``rank`` must be an integer and ``contribution`` an int or finite float (``bool`` / strings are
  rejected as corrupt, never rendered);
* ``inputs/`` — each regular file's SHA-256, checked against ``evidence_digests``;
* ``.seal/manifest.dsse`` — presence only (signature re-verification is ``nova verify``'s job).

Every file is opened for reading only, with ``O_NOFOLLOW`` (shared helper :mod:`._sealed_read`);
nothing under the capsule is created, modified, or deleted. Reads are bounded (manifest /
model-call / facet sizes and counts are capped). A symlinked ``capsule.yaml`` /
``model-calls.jsonl``, a file whose digest ``evidence_digests`` records but which is absent, a
symlink, or not a regular file (sealed evidence that vanished), a recorded digest that does not
match the bytes on disk, a malformed manifest or model-call line, or a malformed attribution facet
raises :class:`CorruptCapsuleError` (CLI exit 2). An *absent and unrecorded* source is a gap the
renderer reports as ``missing`` — never an error.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from novafabric.capture.record_roles import logical_model_calls

from ._sealed_read import CorruptCapsuleError, hash_regular_file, read_sealed, sha256_bytes
from .adverse_action import (
    ATTRIBUTION_FACET_KEY,
    AttributionFacts,
    CapsuleFacts,
    InputFileFacts,
    ModelCallFacts,
    Number,
    ReasonFacts,
)

__all__ = [
    "MAX_INPUT_FILES",
    "MAX_MODEL_CALLS",
    "MAX_REASONS",
    "CorruptCapsuleError",
    "collect_capsule_facts",
]

#: Upper bounds (a credit-decision capsule is KiB–MiB in practice; these stop a hostile one).
MANIFEST_MAX_BYTES = 8 * 1024 * 1024
MODEL_CALLS_MAX_BYTES = 128 * 1024 * 1024
MAX_MODEL_CALLS = 500
MAX_INPUT_FILES = 500
MAX_REASONS = 100
MAX_REASON_TEXT = 1024
#: Cap on any other recorded string the pack renders (model refs, ids, status, timestamps).
MAX_FIELD_TEXT = 1024

_REASON_TEXT_FIELDS = ("feature", "reason_code", "description")
_ATTRIBUTION_TEXT_FIELDS = ("method", "producer", "model_call_id")
_INPUTS_PREFIX = "inputs/"


def _sha256_file(path: Path) -> tuple[str, int]:
    """Stream-hash one input file (no symlinks; ``CorruptCapsuleError`` when unreadable)."""
    return hash_regular_file(path, "input file")


def _canonical_digest(value: Any) -> str:
    """SHA-256 over canonical JSON (sorted keys, compact) of a recorded value."""
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_bytes(data.encode("utf-8"))


def _load_manifest(capsule_dir: Path) -> dict[str, Any]:
    import yaml

    raw = read_sealed(capsule_dir, "capsule.yaml", MANIFEST_MAX_BYTES, "manifest", {})
    if raw is None:
        raise CorruptCapsuleError(f"no capsule.yaml in {capsule_dir}")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CorruptCapsuleError(f"unparseable capsule.yaml in {capsule_dir}: {exc}") from exc
    if not isinstance(data, dict):
        raise CorruptCapsuleError(f"capsule.yaml in {capsule_dir} is not a mapping")
    return data


def _evidence_digests(manifest: dict[str, Any]) -> dict[str, str]:
    """Return ``{capsule-relative path: 'sha256:<hex>'}`` from the manifest (empty if absent)."""
    raw = manifest.get("evidence_digests")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CorruptCapsuleError("capsule.yaml evidence_digests is not a mapping")
    out: dict[str, str] = {}
    for rel, entry in raw.items():
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(digest, str):
            raise CorruptCapsuleError(f"evidence_digests[{rel!r}] carries no sha256")
        out[str(rel)] = digest
    return out


def _redact(
    value: str | None, field: str, suppressed: list[str], rules_hit: list[str]
) -> str | None:
    """Return ``value``, or ``None`` (recording ``field`` + rule ids) when secret-shaped."""
    if value is None:
        return None
    from novafabric.capture.secrets import scan_text_rule_ids

    hits = scan_text_rule_ids(value)
    if not hits:
        return value
    suppressed.append(field)
    rules_hit.extend(h for h in hits if h not in rules_hit)
    return None


def _call_text(
    record: dict[str, Any],
    key: str,
    where: str,
    suppressed: list[str],
    rules_hit: list[str],
) -> str | None:
    """A model-call string field: stringified as recorded, length-capped, secret-scanned."""
    raw = record.get(key)
    if raw is None:
        return None
    if isinstance(raw, dict | list):
        raise CorruptCapsuleError(f"{where}.{key} must be a scalar, got {type(raw).__name__}")
    value = str(raw)
    if len(value) > MAX_FIELD_TEXT:
        raise CorruptCapsuleError(f"{where}.{key} exceeds {MAX_FIELD_TEXT} characters")
    return _redact(value, key, suppressed, rules_hit)


def _read_call(
    record: dict[str, Any], lineno: int, line: bytes, rules_hit: list[str]
) -> ModelCallFacts:
    where = f"model-calls.jsonl line {lineno}"
    suppressed: list[str] = []
    texts = {
        key: _call_text(record, key, where, suppressed, rules_hit)
        for key in (
            "model_call_id",
            "gen_ai.system",
            "gen_ai.request.model",
            "gen_ai.response.model",
            "status",
            "started_at",
        )
    }
    messages = record.get("gen_ai.request.messages")
    choices = record.get("gen_ai.response.choices")
    return ModelCallFacts(
        line=lineno,
        model_call_id=texts["model_call_id"],
        system=texts["gen_ai.system"],
        request_model=texts["gen_ai.request.model"],
        response_model=texts["gen_ai.response.model"],
        status=texts["status"],
        started_at=texts["started_at"],
        input_digest=None if messages is None else _canonical_digest(messages),
        output_digest=None if choices is None else _canonical_digest(choices),
        record_sha256=sha256_bytes(line),
        suppressed_fields=suppressed,
    )


def _read_model_calls(
    capsule_dir: Path, digests: dict[str, str], rules_hit: list[str]
) -> tuple[list[ModelCallFacts], int, bool]:
    """Return ``(calls, total, bound)`` for ``model-calls.jsonl`` (empty when absent+unrecorded).

    Secret-rule ids that fired on a rendered model-call string are appended to ``rules_hit``.
    A stream sealed in ``digests`` that is absent / not a regular file, or any symlink, raises.
    """
    raw = read_sealed(
        capsule_dir, "model-calls.jsonl", MODEL_CALLS_MAX_BYTES, "model-calls stream", digests
    )
    if raw is None:
        return [], 0, False
    recorded = digests.get("model-calls.jsonl")
    calls: list[ModelCallFacts] = []
    total = 0
    parsed: list[tuple[int, bytes, dict[str, Any]]] = []
    for lineno, line in enumerate(raw.split(b"\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CorruptCapsuleError(f"model-calls.jsonl line {lineno} is not JSON") from exc
        if not isinstance(record, dict):
            raise CorruptCapsuleError(f"model-calls.jsonl line {lineno} is not a JSON object")
        parsed.append((lineno, line, record))
    # ADR-0305: logical calls only; a transport record is the wire hook's copy
    # of an HTTP attempt under an SDK call. Line numbers stay the file's own.
    keep = {id(r) for r in logical_model_calls([r for _, _, r in parsed])}
    for lineno, line, record in parsed:
        if id(record) not in keep:
            continue
        total += 1
        if len(calls) >= MAX_MODEL_CALLS:
            continue
        calls.append(_read_call(record, lineno, line, rules_hit))
    return calls, total, recorded is not None


def _read_inputs(capsule_dir: Path, digests: dict[str, str]) -> tuple[list[InputFileFacts], int]:
    """Hash the regular files under ``inputs/`` (symlinks and escapes are never followed).

    Raises:
        CorruptCapsuleError: an ``inputs/`` path sealed in ``digests`` is absent, a symlink, or
            not a regular file; or a hashed file is unreadable / mismatches its recorded digest.
    """
    inputs_dir = capsule_dir / "inputs"
    sealed = sorted(rel for rel in digests if rel.startswith(_INPUTS_PREFIX))
    if not inputs_dir.is_dir() or inputs_dir.is_symlink():
        _require_sealed_inputs_seen(sealed, set())
        return [], 0
    root = capsule_dir.resolve()
    facts: list[InputFileFacts] = []
    seen: set[str] = set()
    total = 0
    for path in sorted(inputs_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            resolved = path.resolve()
        except OSError as exc:  # pragma: no cover - resolve of an existing file
            raise CorruptCapsuleError(f"cannot resolve {path}: {exc}") from exc
        if not resolved.is_relative_to(root):  # pragma: no cover - defensive (3.13+ rglob)
            continue  # a path reached through a symlinked directory is not capsule evidence
        total += 1
        rel = path.relative_to(capsule_dir).as_posix()
        seen.add(rel)
        if len(facts) >= MAX_INPUT_FILES:
            continue
        try:
            digest, size = _sha256_file(path)
        except OSError as exc:
            raise CorruptCapsuleError(f"cannot read input file {rel}: {exc}") from exc
        recorded = digests.get(rel)
        if recorded is not None and recorded != digest:
            raise CorruptCapsuleError(
                f"{rel} does not match its sealed evidence_digests entry (recorded {recorded})"
            )
        facts.append(
            InputFileFacts(path=rel, sha256=digest, size_bytes=size, bound=recorded is not None)
        )
    _require_sealed_inputs_seen(sealed, seen)
    return facts, total


def _require_sealed_inputs_seen(sealed: list[str], seen: set[str]) -> None:
    """Every ``inputs/`` path sealed in ``evidence_digests`` must still be a regular file."""
    for rel in sealed:
        if rel not in seen:
            raise CorruptCapsuleError(
                f"{rel} is sealed in evidence_digests but is absent from the capsule, "
                "a symlink, or not a regular file"
            )


def _rank(value: Any, where: str) -> int | None:
    """A recorded rank: ``None`` or an integer (``bool`` is not an integer here)."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise CorruptCapsuleError(f"{where} must be an integer, got {type(value).__name__}")


def _contribution(value: Any, where: str) -> Number | None:
    """A recorded contribution: ``None``, an integer, or a finite float (never bool/str)."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CorruptCapsuleError(f"{where} must be finite, got {value!r}")
        return value
    raise CorruptCapsuleError(f"{where} must be a number, got {type(value).__name__}")


def _text(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CorruptCapsuleError(f"{where} must be a string")
    if len(value) > MAX_REASON_TEXT:
        raise CorruptCapsuleError(f"{where} exceeds {MAX_REASON_TEXT} characters")
    return value


def _read_reason(item: Any, position: int, rules_hit: list[str]) -> ReasonFacts:
    where = f"facets.{ATTRIBUTION_FACET_KEY}.reasons[{position - 1}]"
    if not isinstance(item, dict):
        raise CorruptCapsuleError(f"{where} is not a mapping")
    suppressed: list[str] = []
    texts = {
        field: _redact(_text(item.get(field), f"{where}.{field}"), field, suppressed, rules_hit)
        for field in _REASON_TEXT_FIELDS
    }
    rank = _rank(item.get("rank"), f"{where}.rank")
    contribution = _contribution(item.get("contribution"), f"{where}.contribution")
    if not suppressed and all(v is None for v in texts.values()):
        raise CorruptCapsuleError(f"{where} names no feature, reason_code, or description")
    return ReasonFacts(
        position=position,
        rank=rank,
        feature=texts["feature"],
        reason_code=texts["reason_code"],
        description=texts["description"],
        contribution=contribution,
        suppressed_fields=suppressed,
    )


def _read_attribution(manifest: dict[str, Any], capsule_ref: str) -> AttributionFacts | None:
    facets = manifest.get("facets")
    if facets is None:
        return None
    if not isinstance(facets, dict):
        raise CorruptCapsuleError("capsule.yaml facets is not a mapping")
    block = facets.get(ATTRIBUTION_FACET_KEY)
    if block is None:
        return None
    where = f"facets.{ATTRIBUTION_FACET_KEY}"
    if not isinstance(block, dict):
        raise CorruptCapsuleError(f"{where} is not a mapping")
    raw_reasons = block.get("reasons", [])
    if not isinstance(raw_reasons, list):
        raise CorruptCapsuleError(f"{where}.reasons is not a list")
    rules_hit: list[str] = []
    suppressed: list[str] = []
    block_texts = {
        field: _redact(_text(block.get(field), f"{where}.{field}"), field, suppressed, rules_hit)
        for field in _ATTRIBUTION_TEXT_FIELDS
    }
    reasons = [
        _read_reason(item, i, rules_hit)
        for i, item in enumerate(raw_reasons[:MAX_REASONS], start=1)
    ]
    return AttributionFacts(
        facet_ref=f"{capsule_ref}/capsule.yaml#{where}",
        method=block_texts["method"],
        producer=block_texts["producer"],
        model_call_id=block_texts["model_call_id"],
        reasons=reasons,
        total_reasons=len(raw_reasons),
        suppressed_fields=suppressed,
        suppressed_rule_ids=rules_hit,
    )


def collect_capsule_facts(capsule_dir: Path) -> CapsuleFacts:
    """Read the NF-278 facts from ``capsule_dir`` (strictly read-only).

    Raises:
        CorruptCapsuleError: the manifest is absent/unreadable/malformed/a symlink, a file
            sealed in ``evidence_digests`` is absent / a symlink / not a regular file, a stream
            exceeds its read bound, a sealed digest does not match the bytes on disk, a
            model-call line is not a JSON object, or the attribution facet is malformed.
    """
    capsule_ref = str(capsule_dir)
    manifest = _load_manifest(capsule_dir)
    digests = _evidence_digests(manifest)
    call_rules_hit: list[str] = []
    calls, total_calls, bound = _read_model_calls(capsule_dir, digests, call_rules_hit)
    inputs, total_inputs = _read_inputs(capsule_dir, digests)
    seal = capsule_dir / ".seal" / "manifest.dsse"
    run_id = manifest.get("run_id") or manifest.get("capsule_id") or capsule_dir.name
    status = manifest.get("status")
    return CapsuleFacts(
        run_id=str(run_id),
        capsule_ref=capsule_ref,
        run_status=None if status is None else str(status),
        model_calls=calls,
        total_model_calls=total_calls,
        model_calls_bound=bound,
        model_call_suppressed_rule_ids=call_rules_hit,
        inputs=inputs,
        total_inputs=total_inputs,
        attribution=_read_attribution(manifest, capsule_ref),
        seal_ref=".seal/manifest.dsse" if seal.is_file() and not seal.is_symlink() else None,
    )
