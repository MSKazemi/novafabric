# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Capture stage: encrypt a data subject's PII under its DEK (ADR-0314, experimental).

``nova capture --pii-subject <id>`` adds this stage to the capture's masking step. It
has the same two entry points as the ADR-0135 :class:`~novafabric.masking.MaskingPipeline`
(``run`` over capsule files, ``mask_mapping`` over the manifest) and walks the same
targets in the same order, so the capture orchestrator calls it exactly where it calls
the masking pipeline and needs no change of its own.

For every string value it detects PII spans (the cap-001 :class:`PIIDetector`, plus
every literal occurrence of the subject id itself) and replaces each span with a
:mod:`novafabric.pii.envelope` token encrypted under the subject's DEK from
:meth:`DEKStore.get_or_create_dek`. Each protected span is recorded in the capsule's
``redaction_manifest.json`` (field path, rule, subject HMAC, ``protection``), which is
what ``nova pii status`` and ``nova pii erase`` read.

Failure posture (never block the workload; never fall back to plaintext):

* the DEK cannot be created or used → every span is **masked** instead
  (``[REDACTED:<subject-hmac>:<rule>]``, data-losing), a structured warning is logged
  once, and a ``masker_errors[]`` entry (``action_taken: redact``) is added per field;
* an unexpected error while rewriting a file → the file is re-processed in mask-only
  mode; if that fails too its content is **dropped** (``action_taken: drop``).

Never raises into capture. Never logs plaintext, the raw subject id or key material.

The capsule is untrusted input by the time this stage runs (after the post-workload
sweep, ``capture/tamper.py``), so every read and write goes through
:mod:`novafabric._capsule_fs`: no link is followed and no FIFO opened, and every file is
rewritten by an atomic rename that keeps its mode bits -- a hard link planted at a
target is replaced, never written through, and ``redaction_manifest.json`` can never be
redirected outside the capsule.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, overload

import yaml

from novafabric._capsule_fs import (
    is_capsule_regular_file,
    open_capsule_file,
    replace_capsule_file,
)
from novafabric.capture import secrets as _secrets
from novafabric.compliance.pii.detector import PIIDetector, PIISpan, RegexDetector
from novafabric.compliance.pii.manifest import RedactionManifest, RedactionManifestEntry
from novafabric.masking._pipeline import _walk, _walk_mode, default_targets
from novafabric.pii.dek.store import DataSubjectDEK, DEKStore, open_dek_store
from novafabric.pii.envelope import (
    TOKEN_RE,
    seal_value,
    subject_hash_prefix,
    subject_ref,
)

logger = logging.getLogger(__name__)

STAGE_ID = "novafabric.pii.envelope"
STAGE_VERSION = "1"
MANIFEST_FILENAME = "redaction_manifest.json"
SUBJECT_ENTITY = "SUBJECT_ID"
#: A subject id shorter than this is not searched for literally (too many false hits).
_MIN_LITERAL_SUBJECT = 4
#: The ``operation`` named in a ``_capsule_fs`` refusal.
_FILE_OPERATION = "the --pii-subject capture stage"

#: Markers earlier stages wrote; a span overlapping one is never re-processed.
_EXISTING_MARKER_RE = re.compile(r"\[(?:NFPII|REDACTED|MASKED)[^\]]*\]")
_ENTITY_SAFE_RE = re.compile(r"[^A-Z0-9_]")


class _MaskOnly(Exception):
    """Internal: the DEK path is unavailable; mask instead."""


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    entity: str


def _entity(name: str) -> str:
    cleaned = _ENTITY_SAFE_RE.sub("_", name.upper())[:32]
    return cleaned or "PII"


class SubjectEnvelopeStage:
    """Encrypts one identified data subject's PII under its per-subject DEK.

    Args:
        subject_id: The data subject this capture's PII belongs to. Stored only in
            ``dek.db`` (outside the capsule); the capsule carries its HMAC.
        pepper: ``NOVA_PII_PEPPER`` bytes for the subject HMAC. Required.
        detector: cap-001 detector; defaults to the regex detector.
        dek_store_factory: opens the DEK store; defaults to
            ``$NOVAFABRIC_HOME/dek.db``.
        legal_basis: recorded on each manifest entry.
    """

    def __init__(
        self,
        subject_id: str,
        *,
        pepper: bytes,
        detector: PIIDetector | None = None,
        dek_store_factory: Callable[[], DEKStore] | None = None,
        tenant_id: str = "default",
        legal_basis: str = "operator_declared_compliance",
    ) -> None:
        if not subject_id:
            raise ValueError("subject_id must be a non-empty string")
        if not pepper:
            raise ValueError(
                "NOVA_PII_PEPPER is required to reference the subject without storing it"
            )
        self._subject_id = subject_id
        self._ref = subject_ref(pepper, subject_id)
        self._detector: PIIDetector = detector or RegexDetector()
        self._factory = dek_store_factory or open_dek_store
        self._tenant_id = tenant_id
        self._legal_basis = legal_basis
        self._dek: DataSubjectDEK | None = None
        self._dek_failed = False
        self._entries: list[RedactionManifestEntry] = []

    # -- DEK ---------------------------------------------------------------

    def _get_dek(self) -> DataSubjectDEK:
        if self._dek is not None:
            return self._dek
        if self._dek_failed:
            raise _MaskOnly()
        store: DEKStore | None = None
        try:
            store = self._factory()
            self._dek = store.get_or_create_dek(self._subject_id, tenant_id=self._tenant_id)
        except Exception as exc:  # noqa: BLE001 — degrade to masking, never plaintext
            self._dek_failed = True
            logger.warning(
                "pii_envelope_dek_unavailable",
                extra={
                    "event": "pii_envelope_dek_unavailable",
                    "subject_sha256_prefix": subject_hash_prefix(self._subject_id),
                    "error_type": type(exc).__name__,
                    "fallback": "mask",
                },
            )
            raise _MaskOnly() from None
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    pass
        return self._dek

    # -- spans ---------------------------------------------------------------

    def _spans(self, value: str) -> list[_Span]:
        blocked = [(m.start(), m.end()) for m in _EXISTING_MARKER_RE.finditer(value)]
        found: list[PIISpan] = list(self._detector.detect(value))
        if len(self._subject_id) >= _MIN_LITERAL_SUBJECT:
            start = value.find(self._subject_id)
            while start != -1:
                end = start + len(self._subject_id)
                found.append(PIISpan(start=start, end=end, entity_type=SUBJECT_ENTITY, score=1.0))
                start = value.find(self._subject_id, end)

        def _keep(s: PIISpan) -> bool:
            if s.start < 0 or s.end > len(value) or s.end <= s.start:
                return False
            if any(s.start < b_end and b_start < s.end for b_start, b_end in blocked):
                return False
            # A match glued to letters/digits is part of a larger token (a digest, an
            # id, a ciphertext group) -- not a PII value.
            if s.start > 0 and value[s.start - 1].isalnum():
                return False
            return not (s.end < len(value) and value[s.end].isalnum())

        kept = sorted((s for s in found if _keep(s)), key=lambda s: (s.start, -s.end))
        merged: list[_Span] = []
        for s in kept:
            ent = _entity(s.entity_type)
            if merged and s.start < merged[-1].end:
                last = merged[-1]
                ents = last.entity.split("+")
                if ent not in ents:
                    ents.append(ent)
                merged[-1] = _Span(last.start, max(last.end, s.end), "+".join(ents)[:64])
            else:
                merged.append(_Span(s.start, s.end, ent))
        return merged

    # -- one value -------------------------------------------------------------

    def _protect(
        self, value: str, ref: str, run_id: str, errors: list[dict[str, Any]],
        *, mask_only: bool = False,
    ) -> str:
        spans = self._spans(value)
        if not spans:
            return value
        dek: DataSubjectDEK | None = None
        if not mask_only:
            try:
                dek = self._get_dek()
            except _MaskOnly:
                dek = None
        if dek is None:
            errors.append(self._error(ref, "redact"))
        now = datetime.now(timezone.utc).isoformat()
        out: list[str] = []
        cursor = 0
        envelopes_before = 0
        protection: Literal["envelope", "mask"]
        for span in spans:
            prefix = value[cursor:span.start]
            envelopes_before += len(TOKEN_RE.findall(prefix))
            out.append(prefix)
            plaintext = value[span.start:span.end]
            if dek is not None:
                token = seal_value(
                    plaintext,
                    dek=dek,
                    subject_ref_value=self._ref,
                    run_id=run_id,
                    field_path=ref,
                    span_index=envelopes_before,
                    entity_type=span.entity,
                ).to_token()
                envelopes_before += 1
                protection = "envelope"
            else:
                token = f"[REDACTED:{self._ref}:{span.entity}]"
                protection = "mask"
            out.append(token)
            cursor = span.end
            self._entries.append(
                RedactionManifestEntry(
                    field_path=ref,
                    detection_rule_id=span.entity,
                    legal_basis=self._legal_basis,
                    subject_id_hmac=self._ref,
                    redacted_at_utc=now,
                    protection=protection,
                )
            )
        out.append(value[cursor:])
        return "".join(out)

    def _error(self, ref: str, action: str) -> dict[str, Any]:
        return {
            "masker_id": STAGE_ID,
            "masker_version": STAGE_VERSION,
            "target_ref": ref,
            "reason": "raised",
            "action_taken": action,
            "detail_hash": None,
        }

    # -- pipeline interface (duck-typed like MaskingPipeline) -------------------

    def run(
        self,
        capsule_dir: Path,
        run_id: str,
        targets: list[tuple[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Protect every scan target in *capsule_dir*. Returns ``([], errors)``.

        No ``masker_findings[]`` are emitted: a finding carries ``match_hash`` (a
        SHA-256 of the matched value), and a hash of a low-entropy PII value would
        survive the erasure it is meant to support (EDPB 01/2025).
        """
        errors: list[dict[str, Any]] = []
        walk = default_targets(capsule_dir) if targets is None else targets
        for filename, _kind in walk:
            if not is_capsule_regular_file(capsule_dir, filename):
                continue
            try:
                self._file(capsule_dir, filename, run_id, errors, mask_only=False)
            except Exception:  # noqa: BLE001 — must not crash capture
                logger.warning(
                    "pii_envelope_file_failed",
                    extra={"event": "pii_envelope_file_failed", "target_ref": filename},
                )
                try:
                    self._file(capsule_dir, filename, run_id, errors, mask_only=True)
                    errors.append(self._error(filename, "redact"))
                except Exception:  # noqa: BLE001 — last resort: drop, never plaintext
                    try:
                        replace_capsule_file(
                            capsule_dir, filename, b"", operation=_FILE_OPERATION
                        )
                    finally:
                        errors.append(self._error(filename, "drop"))
        self._write_manifest(capsule_dir, run_id)
        return [], errors

    def mask_mapping(
        self, obj: dict[str, Any], ref: str, kind: str, run_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """Protect every string of an in-memory mapping (the manifest). Never raises."""
        errors: list[dict[str, Any]] = []

        def apply(path: str, value: str) -> str:
            locator = ref + (f" {path}" if path else "")
            return self._protect(value, locator, run_id, errors)

        try:
            new_obj, _changed = _walk(obj, "", apply)
        except Exception:  # noqa: BLE001
            logger.warning(
                "pii_envelope_manifest_failed",
                extra={"event": "pii_envelope_manifest_failed", "target_ref": ref},
            )

            def mask(path: str, value: str) -> str:
                locator = ref + (f" {path}" if path else "")
                return self._protect(value, locator, run_id, errors, mask_only=True)

            new_obj, _changed = _walk(obj, "", mask)
            errors.append(self._error(ref, "redact"))
        return new_obj, [], errors

    # -- files ---------------------------------------------------------------

    def _file(
        self, root: Path, filename: str, run_id: str, errors: list[dict[str, Any]],
        *, mask_only: bool,
    ) -> None:
        mode = _walk_mode(filename)
        if mode == "jsonl":
            self._jsonl(root, filename, run_id, errors, mask_only)
        elif mode == "yaml":
            data, file_mode = _read(root, filename)
            obj = yaml.safe_load(data.decode("utf-8"))
            if not isinstance(obj, (dict, list)):
                return

            def apply(p: str, v: str) -> str:
                locator = filename + (f" {p}" if p else "")
                return self._protect(v, locator, run_id, errors, mask_only=mask_only)

            new_obj, changed = _walk(obj, "", apply)
            if changed:
                _rewrite(root, filename, yaml.dump(new_obj, allow_unicode=True), file_mode)
        else:
            self._text(root, filename, run_id, errors, mask_only)

    def _jsonl(
        self, root: Path, filename: str, run_id: str, errors: list[dict[str, Any]],
        mask_only: bool,
    ) -> None:
        data, file_mode = _read(root, filename)
        raw = data.decode("utf-8")
        out: list[str] = []
        changed_any = False
        for lineno, line in enumerate(raw.splitlines(keepends=True), start=1):
            stripped = line.strip()
            if not stripped:
                out.append(line)
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                # Unparseable: protect it as one text value so PII cannot survive.
                body = line.rstrip("\r\n")
                new = self._protect(body, f"{filename}#L{lineno}", run_id, errors,
                                    mask_only=mask_only)
                changed_any = changed_any or new != body
                out.append(new + line[len(body):])
                continue

            def apply(p: str, v: str, _n: int = lineno) -> str:
                locator = f"{filename}#L{_n}" + (f" {p}" if p else "")
                return self._protect(v, locator, run_id, errors, mask_only=mask_only)

            new_obj, changed = _walk(obj, "", apply)
            if changed:
                out.append(json.dumps(new_obj) + ("\n" if line.endswith("\n") else ""))
                changed_any = True
            else:
                out.append(line)
        if changed_any:
            _rewrite(root, filename, "".join(out), file_mode)

    def _text(
        self, root: Path, filename: str, run_id: str, errors: list[dict[str, Any]],
        mask_only: bool,
    ) -> None:
        read = _read(root, filename, max_bytes=_secrets.MAX_ARTIFACT_SCAN_BYTES)
        if read is None:
            return  # over the artifact bound: the built-in scanner records it
        data, file_mode = read
        if _secrets._is_binary(data):
            return
        out: list[str] = []
        changed = False
        for lineno, line in enumerate(data.decode("utf-8").splitlines(keepends=True), start=1):
            body = line.rstrip("\r\n")
            new = self._protect(body, f"{filename}#L{lineno}", run_id, errors,
                                mask_only=mask_only)
            changed = changed or new != body
            out.append(new + line[len(body):])
        if changed:
            _rewrite(root, filename, "".join(out), file_mode)

    def _write_manifest(self, capsule_dir: Path, run_id: str) -> None:
        if not self._entries:
            return
        manifest = RedactionManifest(capsule_id=run_id, entries=list(self._entries))
        replace_capsule_file(
            capsule_dir,
            MANIFEST_FILENAME,
            manifest.model_dump_json(indent=2).encode("utf-8"),
            operation=_FILE_OPERATION,
        )


@overload
def _read(root: Path, rel: str) -> tuple[bytes, int]: ...
@overload
def _read(root: Path, rel: str, *, max_bytes: int) -> tuple[bytes, int] | None: ...
def _read(root: Path, rel: str, *, max_bytes: int | None = None) -> tuple[bytes, int] | None:
    """``(bytes, mode bits)`` of capsule file *rel*, following no link, never a FIFO.

    ``None`` when *max_bytes* is given and the file is larger.
    """
    with open_capsule_file(root, rel, operation=_FILE_OPERATION) as handle:
        st = os.fstat(handle.fileno())
        if max_bytes is not None and st.st_size > max_bytes:
            return None
        return handle.read(), st.st_mode & 0o7777


def _rewrite(root: Path, rel: str, text: str, file_mode: int) -> None:
    """Replace capsule file *rel* by a rename (never written through), keeping its mode."""
    replace_capsule_file(
        root, rel, text.encode("utf-8"), operation=_FILE_OPERATION, file_mode=file_mode
    )


class ChainedCaptureStages:
    """Run several masking-pipeline-shaped stages in order, as one pipeline.

    Lets ``nova capture`` add :class:`SubjectEnvelopeStage` after the operator's
    ADR-0135 maskers without changing the orchestrator's single
    ``masking_pipeline`` slot.
    """

    def __init__(self, stages: list[Any]) -> None:
        self._stages = [s for s in stages if s is not None]

    def run(
        self,
        capsule_dir: Path,
        run_id: str,
        targets: list[tuple[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        findings: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for stage in self._stages:
            f, e = stage.run(capsule_dir, run_id, targets=targets)
            findings.extend(f)
            errors.extend(e)
        return findings, errors

    def mask_mapping(
        self, obj: dict[str, Any], ref: str, kind: str, run_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        findings: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for stage in self._stages:
            obj, f, e = stage.mask_mapping(obj, ref, kind, run_id)
            findings.extend(f)
            errors.extend(e)
        return obj, findings, errors
