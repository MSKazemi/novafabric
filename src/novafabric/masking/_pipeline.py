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
import os
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from novafabric._capsule_fs import open_capsule_file, read_capsule_text, replace_capsule_file
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


#: One queued masker call: its future, the callable and its arguments.
_Job = tuple[Future[Any], Callable[..., Any], tuple[Any, ...]]


class _DaemonWorker:
    """One daemon thread running masker calls in order; never joined at interpreter exit."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        threading.Thread(target=self._loop, name="nova-masker", daemon=True).start()

    def _loop(self) -> None:
        while (item := self._queue.get()) is not None:
            future, fn, args = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args))
            except BaseException as exc:  # noqa: BLE001 — handed to the caller
                future.set_exception(exc)

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        future: Future[Any] = Future()
        self._queue.put((future, fn, args))
        return future

    def close(self) -> None:
        self._queue.put(None)


class MaskingPipeline:
    """Ordered chain of operator-registered maskers (ADR-0135 D3–D5)."""

    def __init__(self, maskers: list[LoadedMasker], tenant: str = "local") -> None:
        self._maskers = list(maskers)
        self._tenant = tenant
        self._worker: _DaemonWorker | None = None
        #: Maskers that overran their budget once: skipped (fail-closed) from then on.
        self._timed_out: set[str] = set()

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
                    # Read and rewrite through the no-follow helpers: the capsule
                    # is untrusted once the workload has run.
                    if mode == "jsonl":
                        self._mask_jsonl(capsule_dir, filename, kind, run_id, findings, errors)
                    elif mode == "yaml":
                        self._mask_yaml(capsule_dir, filename, kind, run_id, findings, errors)
                    else:
                        self._mask_text(capsule_dir, filename, kind, run_id, findings, errors)
                except Exception:  # noqa: BLE001 — must not crash capture
                    logger.exception(
                        "novafabric.masking: pipeline failed on %s; masking it as text", filename
                    )
                    # Fail closed (compliance-privacy-05): the error used to say
                    # "redact" while the file was left as it was. Mask it as plain
                    # text; when even that cannot run, withhold its content.
                    action = self._fail_closed_file(
                        capsule_dir, filename, kind, run_id, findings, errors
                    )
                    errors.append({
                        "masker_id": _PIPELINE_ID,
                        "masker_version": _PIPELINE_VERSION,
                        "target_ref": filename,
                        "reason": "raised",
                        "action_taken": action,
                        "detail_hash": None,
                    })
        finally:
            self._close()
        return findings, errors

    def _fail_closed_file(
        self,
        root: Path,
        filename: str,
        kind: str,
        run_id: str,
        findings: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> str:
        """After a file's structured masking raised: mask it as text, else withhold it.

        Returns the ``action_taken`` to record — ``redact`` when the text pass ran,
        ``drop`` when the file's content was replaced by a placeholder (binary,
        oversize, or the text pass failed too). The schema allows only these two: a
        masking error never leaves the value in place.
        """
        try:
            if self._mask_text(root, filename, kind, run_id, findings, errors):
                return "redact"
        except Exception:  # noqa: BLE001 — fall through to withholding the file
            logger.exception("novafabric.masking: text fallback failed on %s", filename)
        _rewrite(root, filename, f"[novafabric: {filename} withheld — masking failed]\n")
        return "drop"

    # -- per-file walkers ---------------------------------------------------

    def _mask_jsonl(
        self,
        root: Path,
        filename: str,
        kind: str,
        run_id: str,
        findings: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> None:
        raw = read_capsule_text(root, filename, operation=_FILE_OPERATION)
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

            def apply(json_path: str, value: str, _n: int = lineno, _o: int = line_start) -> str:
                ref = f"{filename}#L{_n}" + (f" {json_path}" if json_path else "")
                field = MaskField(target_kind=kind, target_ref=ref, byte_offset=_o)
                return self._apply_chain(field, value, run_id, findings, errors)

            try:
                obj = json.loads(stripped)
                new_obj, changed = _walk(obj, "", apply)
            except (ValueError, RecursionError):
                # Not JSON that can be walked — malformed, or nested past the
                # recursion limit (tool arguments are arbitrary JSON). Mask the line
                # as text: passing it through left its values unmasked, and a
                # RecursionError used to abort the whole file (compliance-privacy-05).
                body = line.rstrip("\r\n")
                masked = apply("", body)
                out_lines.append(masked + line[len(body):])
                changed_any = changed_any or masked != body
                continue
            if changed:
                newline = "\n" if line.endswith("\n") else ""
                out_lines.append(json.dumps(new_obj) + newline)
                changed_any = True
            else:
                out_lines.append(line)
        if changed_any:
            _rewrite(root, filename, "".join(out_lines))

    def _mask_yaml(
        self,
        root: Path,
        filename: str,
        kind: str,
        run_id: str,
        findings: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> None:
        obj = yaml.safe_load(read_capsule_text(root, filename, operation=_FILE_OPERATION))
        if not isinstance(obj, (dict, list)):
            return

        def apply(yaml_path: str, value: str) -> str:
            ref = f"{filename}" + (f" {yaml_path}" if yaml_path else "")
            field = MaskField(target_kind=kind, target_ref=ref, byte_offset=0)
            return self._apply_chain(field, value, run_id, findings, errors)

        new_obj, changed = _walk(obj, "", apply)
        if changed:
            _rewrite(root, filename, yaml.dump(new_obj, allow_unicode=True))

    def _mask_text(
        self,
        root: Path,
        filename: str,
        kind: str,
        run_id: str,
        findings: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> bool:
        """Mask a free-text artifact (``inputs/**``, ``outputs/**``) one line at a time.

        Each line, without its terminator, is one value (ref ``<file>#L<n>``). A
        binary or oversize artifact is skipped: maskers take strings, never raw
        blobs (ADR-0135 D1), and the built-in scanner already covers both.
        Returns whether the file was scanned (False for a skipped one).
        """
        with open_capsule_file(root, filename, operation=_FILE_OPERATION) as fh:
            if os.fstat(fh.fileno()).st_size > _secrets.MAX_ARTIFACT_SCAN_BYTES:
                return False
            data = fh.read()
        if _secrets._is_binary(data):
            return False
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
            _rewrite(root, filename, "".join(out))
        return True

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
        """Run one masker call under its ``timeout_ms`` budget (D5).

        A masker that overran its budget once is not called again: every later
        value it would see times out at once (fail-closed). Calls run on a daemon
        thread, which the interpreter never waits for at exit — the
        ``ThreadPoolExecutor`` used before was joined by an exit hook whatever
        ``shutdown(wait=False)`` said, and a fresh one per timed-out value left one
        stuck thread per value: ``nova capture`` could never exit
        (compliance-privacy-06).
        """
        masker_id = loaded.spec.id
        if masker_id in self._timed_out:
            raise _MaskerTimeout()
        if self._worker is None:
            self._worker = _DaemonWorker()
        future = self._worker.submit(loaded.masker.mask, field, value, context)
        try:
            return future.result(timeout=loaded.spec.timeout_ms / 1000.0)
        except TimeoutError:
            if future.done():
                raise  # the masker itself raised TimeoutError → reason "raised"
            # Abandon the (stuck) worker — a daemon, so it never blocks exit — and
            # stop calling this masker; others get a fresh worker on their next call.
            self._worker = None
            self._timed_out.add(masker_id)
            raise _MaskerTimeout() from None

    def _close(self) -> None:
        if self._worker is not None:
            self._worker.close()
            self._worker = None


#: Files parsed as structured data; anything else under ``inputs/`` / ``outputs/``
#: is free text (one value per line) even when it happens to be YAML or JSON --
#: re-serializing an arbitrary workload artifact would rewrite bytes nobody masked.
_STRUCTURED_YAML: frozenset[str] = frozenset({"capsule.yaml", "env.lock"})
_STRUCTURED_JSONL: frozenset[str] = frozenset(
    name for name, _ in SCAN_TARGETS + ARTIFACT_SCAN_TARGETS if name.endswith(".jsonl")
)
_TEXT_DIRS: frozenset[str] = frozenset(name for name, _ in ARTIFACT_SCAN_DIRS)


#: The ``operation`` named in a ``_capsule_fs`` refusal.
_FILE_OPERATION = "the masking pipeline"


def _rewrite(root: Path, filename: str, text: str) -> None:
    """Replace capsule file *filename* with *text*, never through a planted link."""
    replace_capsule_file(root, filename, text.encode("utf-8"), operation=_FILE_OPERATION)


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
