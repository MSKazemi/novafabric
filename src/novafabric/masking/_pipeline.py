"""The masking pipeline: bounded, fail-closed masker execution (ADR-0135).

Runs at capture, over the same targets the ADR-0009 secret scanner walks --
the structured streams (``SCAN_TARGETS``), the artifact files
(``env.lock``, ``assets.jsonl``, ``lineage.jsonl``) and every text file under
``inputs/`` and ``outputs/`` (one value per line) -- plus the manifest, via
:meth:`MaskingPipeline.mask_mapping`, before ``capsule.yaml`` is written.
Binary and oversize artifacts are not offered to maskers (a masker takes a
string, never a raw blob -- ADR-0135 D1); the built-in scanner still covers
them. It runs **after** the built-in scanners and **before** the capsule is
finalized; the orchestrator's final ADR-0009 residual pass runs after it, so
the built-in rules always have the last word over masker output.
Built-in redaction is never disabled — maskers observe already-redacted
markers. Every mask a masker applies is attributed in the redaction proof
(``masker_findings[]``); every failure/abort is recorded
(``masker_errors[]``) and the affected field is fail-closed (redacted or
dropped, per ``on_error``) — a masker error never un-redacts, never writes
a raw value, and never crashes or blocks the captured workload.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from novafabric.capture import secrets as _secrets
from novafabric.capture._ulid import new_ulid
from novafabric.capture.secrets import (
    ARTIFACT_SCAN_DIRS,
    ARTIFACT_SCAN_TARGETS,
    SCAN_TARGETS,
    iter_artifact_targets,
)
from novafabric.masking._models import UNCHANGED, MaskContext, MaskField
from novafabric.masking._registry import LoadedMasker

logger = logging.getLogger(__name__)

_PIPELINE_ID = "novafabric.masking.pipeline"
_PIPELINE_VERSION = "0.1.0"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _MaskerTimeout(Exception):
    """Internal: a masker call exceeded its ``timeout_ms`` budget."""


class MaskingPipeline:
    """Ordered chain of operator-registered maskers (ADR-0135 D3–D5)."""

    def __init__(self, maskers: list[LoadedMasker], tenant: str = "local") -> None:
        self._maskers = list(maskers)
        self._tenant = tenant
        self._executor: ThreadPoolExecutor | None = None

    # -- public entry point -------------------------------------------------

    def run(
        self,
        capsule_dir: Path,
        run_id: str,
        targets: list[tuple[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Mask every scan target in ``capsule_dir``; return (findings, errors).

        ``targets`` (capsule-relative ``(ref, kind)`` pairs) restricts the walk,
        e.g. to a file written after the main pass; by default every target the
        built-in scanner walks is masked (:func:`default_targets`).

        Never raises: an unexpected pipeline failure is logged, recorded in
        the errors list, and capture continues (fail-safe for the workload).
        """
        findings: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if not self._maskers:
            return findings, errors
        try:
            walk = default_targets(capsule_dir) if targets is None else targets
            for filename, kind in walk:
                path = capsule_dir / filename
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    mode = _walk_mode(filename)
                    if mode == "jsonl":
                        self._mask_jsonl(path, filename, kind, run_id, findings, errors)
                    elif mode == "yaml":
                        self._mask_yaml(path, filename, kind, run_id, findings, errors)
                    else:
                        self._mask_text(path, filename, kind, run_id, findings, errors)
                except Exception:  # noqa: BLE001 — must not crash capture
                    logger.exception(
                        "novafabric.masking: pipeline failed on %s; continuing", filename
                    )
                    errors.append({
                        "masker_id": _PIPELINE_ID,
                        "masker_version": _PIPELINE_VERSION,
                        "target_ref": filename,
                        "reason": "raised",
                        "action_taken": "redact",
                        "detail_hash": None,
                    })
        finally:
            self._close()
        return findings, errors

    # -- per-file walkers ---------------------------------------------------

    def _mask_jsonl(
        self,
        path: Path,
        filename: str,
        kind: str,
        run_id: str,
        findings: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> None:
        raw = path.read_text(encoding="utf-8")
        out_lines: list[str] = []
        changed_any = False
        byte_pos = 0
        for lineno, line in enumerate(raw.splitlines(keepends=True), start=1):
            line_start = byte_pos
            byte_pos += len(line.encode("utf-8"))
            stripped = line.strip()
            if not stripped:
                out_lines.append(line)
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                out_lines.append(line)  # unparseable: leave the scanner's output as-is
                continue

            def apply(json_path: str, value: str, _n: int = lineno, _o: int = line_start) -> str:
                ref = f"{filename}#L{_n}" + (f" {json_path}" if json_path else "")
                field = MaskField(target_kind=kind, target_ref=ref, byte_offset=_o)
                return self._apply_chain(field, value, run_id, findings, errors)

            new_obj, changed = _walk(obj, "", apply)
            if changed:
                newline = "\n" if line.endswith("\n") else ""
                out_lines.append(json.dumps(new_obj) + newline)
                changed_any = True
            else:
                out_lines.append(line)
        if changed_any:
            path.write_text("".join(out_lines), encoding="utf-8")

    def _mask_yaml(
        self,
        path: Path,
        filename: str,
        kind: str,
        run_id: str,
        findings: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> None:
        obj = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(obj, (dict, list)):
            return

        def apply(yaml_path: str, value: str) -> str:
            ref = f"{filename}" + (f" {yaml_path}" if yaml_path else "")
            field = MaskField(target_kind=kind, target_ref=ref, byte_offset=0)
            return self._apply_chain(field, value, run_id, findings, errors)

        new_obj, changed = _walk(obj, "", apply)
        if changed:
            path.write_text(yaml.dump(new_obj, allow_unicode=True), encoding="utf-8")

    def _mask_text(
        self,
        path: Path,
        filename: str,
        kind: str,
        run_id: str,
        findings: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> None:
        """Mask a free-text artifact (``inputs/**``, ``outputs/**``) one line at a time.

        Each line, without its terminator, is one value (ref ``<file>#L<n>``). A
        binary or oversize artifact is skipped: maskers take strings, never raw
        blobs (ADR-0135 D1), and the built-in scanner already covers both.
        """
        if path.stat().st_size > _secrets.MAX_ARTIFACT_SCAN_BYTES:
            return
        data = path.read_bytes()
        if _secrets._is_binary(data):
            return
        out: list[str] = []
        changed = False
        byte_pos = 0
        for lineno, line in enumerate(data.decode("utf-8").splitlines(keepends=True), start=1):
            line_start = byte_pos
            byte_pos += len(line.encode("utf-8"))
            body = line.rstrip("\r\n")
            if not body.strip():
                out.append(line)
                continue
            field = MaskField(
                target_kind=kind, target_ref=f"{filename}#L{lineno}", byte_offset=line_start
            )
            new = self._apply_chain(field, body, run_id, findings, errors)
            changed = changed or new != body
            out.append(new + line[len(body):])
        if changed:
            path.write_bytes("".join(out).encode("utf-8"))

    def mask_mapping(
        self, obj: dict[str, Any], ref: str, kind: str, run_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """Mask every string value of an in-memory mapping (the manifest).

        ``capsule.yaml`` is written from a data structure, after the main pass,
        so maskers see it here -- before it is written or signed -- exactly as the
        built-in scanner's :meth:`SecretScannerV0.redact_manifest` does. Returns
        ``(new_obj, findings, errors)``; the input is not mutated. Never raises.
        """
        findings: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if not self._maskers:
            return obj, findings, errors

        def apply(path: str, value: str) -> str:
            field = MaskField(
                target_kind=kind, target_ref=ref + (f" {path}" if path else ""), byte_offset=0
            )
            return self._apply_chain(field, value, run_id, findings, errors)

        try:
            new_obj, _changed = _walk(obj, "", apply)
        except Exception:  # noqa: BLE001 — must not crash capture
            logger.exception("novafabric.masking: pipeline failed on %s; continuing", ref)
            errors.append({
                "masker_id": _PIPELINE_ID,
                "masker_version": _PIPELINE_VERSION,
                "target_ref": ref,
                "reason": "raised",
                "action_taken": "redact",
                "detail_hash": None,
            })
            return obj, findings, errors
        finally:
            self._close()
        return new_obj, findings, errors

    # -- masker chain -------------------------------------------------------

    def _apply_chain(
        self,
        field: MaskField,
        value: str,
        run_id: str,
        findings: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> str:
        """Run every masker over one value in declared order (ADR-0135 D3)."""
        current = value
        applied = 0
        for loaded in self._maskers:
            spec = loaded.spec
            if len(current.encode("utf-8")) > spec.max_input_bytes:
                current = self._fail_closed(loaded, field, "oversize", None, errors)
                continue
            context = MaskContext(
                run_id=run_id,
                tenant=self._tenant,
                masker_config=MappingProxyType(spec.config),
            )
            try:
                result = self._call_bounded(loaded, field, current, context)
            except _MaskerTimeout:
                current = self._fail_closed(loaded, field, "timeout", None, errors)
                continue
            except Exception as exc:  # noqa: BLE001 — fail-closed, never crash capture
                current = self._fail_closed(loaded, field, "raised", exc, errors)
                continue
            if result is UNCHANGED or result == current:
                continue  # declined / no-op: no finding (spec §Edge cases)
            if not isinstance(result, str) or (current and current in result):
                # Not a string, or the pre-mask value leaks through verbatim.
                current = self._fail_closed(loaded, field, "declined_invalid", None, errors)
                continue
            findings.append({
                "finding_id": new_ulid(),
                "masker_id": loaded.masker.masker_id,
                "masker_version": loaded.masker.masker_version,
                "pattern_id": str(loaded.masker.pattern_ids[0]),
                "target_kind": field.target_kind,
                "target_ref": field.target_ref,
                "byte_offset": field.byte_offset,
                "byte_length": len(current.encode("utf-8")),
                "match_hash": _sha256(current.encode("utf-8")),
                "redaction_strategy": "mask",
                "replacement": result,
                "chain_position": applied,
            })
            applied += 1
            current = result
        return current

    def _fail_closed(
        self,
        loaded: LoadedMasker,
        field: MaskField,
        reason: str,
        exc: BaseException | None,
        errors: list[dict[str, Any]],
    ) -> str:
        """Apply the most restrictive action and record the failure (D5)."""
        masker_id = loaded.masker.masker_id
        errors.append({
            "masker_id": masker_id,
            "masker_version": loaded.masker.masker_version,
            "target_ref": field.target_ref,
            "reason": reason,
            "action_taken": loaded.spec.on_error,
            "detail_hash": _sha256(repr(exc).encode("utf-8")) if exc is not None else None,
        })
        logger.warning(
            "novafabric.masking: masker %r %s on %s; field %sed (fail-closed)",
            masker_id, reason, field.target_ref, loaded.spec.on_error,
        )
        if loaded.spec.on_error == "drop":
            return ""
        return f"[MASKED:{masker_id}]"

    # -- bounded execution --------------------------------------------------

    def _call_bounded(
        self, loaded: LoadedMasker, field: MaskField, value: str, context: MaskContext
    ) -> Any:
        """Run one masker call under its ``timeout_ms`` budget (D5)."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="nova-masker"
            )
        future = self._executor.submit(loaded.masker.mask, field, value, context)
        try:
            return future.result(timeout=loaded.spec.timeout_ms / 1000.0)
        except TimeoutError:
            if future.done():
                raise  # the masker itself raised TimeoutError → reason "raised"
            # Abandon the (possibly stuck) worker; a fresh executor is created
            # lazily on the next call. The stuck thread cannot block capture.
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
            raise _MaskerTimeout() from None

    def _close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None


#: Files parsed as structured data; anything else under ``inputs/`` / ``outputs/``
#: is free text (one value per line) even when it happens to be YAML or JSON --
#: re-serializing an arbitrary workload artifact would rewrite bytes nobody masked.
_STRUCTURED_YAML: frozenset[str] = frozenset({"capsule.yaml", "env.lock"})
_STRUCTURED_JSONL: frozenset[str] = frozenset(
    name for name, _ in SCAN_TARGETS + ARTIFACT_SCAN_TARGETS if name.endswith(".jsonl")
)
_TEXT_DIRS: frozenset[str] = frozenset(name for name, _ in ARTIFACT_SCAN_DIRS)


def _walk_mode(ref: str) -> str:
    """``jsonl`` | ``yaml`` | ``text`` for a capsule-relative ref."""
    if ref in _STRUCTURED_JSONL:
        return "jsonl"
    if ref in _STRUCTURED_YAML:
        return "yaml"
    if ref.split("/", 1)[0] in _TEXT_DIRS:
        return "text"
    return "jsonl" if ref.endswith(".jsonl") else "yaml"


def default_targets(capsule_dir: Path) -> list[tuple[str, str]]:
    """Every ``(ref, kind)`` the built-in ADR-0009 scanner walks in ``capsule_dir``.

    The structured streams, then the artifact targets in the scanner's own order
    (:func:`novafabric.capture.secrets.iter_artifact_targets`) -- one enumeration,
    so the masking walk and the built-in walk cannot drift apart.
    """
    walk = list(SCAN_TARGETS)
    walk.extend((ref, kind) for _path, ref, kind in iter_artifact_targets(capsule_dir))
    return walk


def _walk(
    obj: Any, path: str, apply: Callable[[str, str], str]
) -> tuple[Any, bool]:
    """Walk structured string values; return (possibly new obj, changed)."""
    if isinstance(obj, str):
        new = apply(path, obj)
        return new, new != obj
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        changed = False
        for key, val in obj.items():
            child = f"{path}.{key}" if path else str(key)
            new_val, val_changed = _walk(val, child, apply)
            out[key] = new_val
            changed = changed or val_changed
        return (out if changed else obj), changed
    if isinstance(obj, list):
        items: list[Any] = []
        changed = False
        for i, val in enumerate(obj):
            new_val, val_changed = _walk(val, f"{path}[{i}]", apply)
            items.append(new_val)
            changed = changed or val_changed
        return (items if changed else obj), changed
    return obj, False
