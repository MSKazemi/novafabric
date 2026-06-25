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
"""PII Detection Gate (cap-001).

OQ-01 (GDPR Art.17) is RESOLVED by ADR-0069 (AES-256-GCM DEK lifecycle).
cap-001 is ACTIVE (graduated from LEGAL-HOLD DRAFT).

Security constraints:
- The HMAC pepper MUST come from NOVA_PII_PEPPER env var.
- The pepper MUST NOT appear in any capsule, log, or config file.
- PIIScanError is NEVER swallowed — it aborts the seal pipeline.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .detector import PIIDetector, PIISpan
from .manifest import RedactionManifest, RedactionManifestEntry

logger = logging.getLogger(__name__)

# OQ-01 resolved by ADR-0069 (AES-256-GCM DEK lifecycle, 2026-05-27).
# cap-001 is now active; legal_hold mode is disabled.
_OQ01_UNRESOLVED = False


class PIIScanError(Exception):
    """Raised when the PII scanner fails.  NEVER swallowed — aborts the seal pipeline."""


@dataclass
class RedactedPayload:
    """Result of a PII scan/redaction pass."""

    original_fields: list[str]
    """Field paths that were scanned."""

    redacted_count: int
    """Number of PII spans replaced."""

    payload: dict[str, Any]
    """The payload dict with PII spans replaced by ``[REDACTED:<hmac>:<type>]`` tokens."""

    manifest: RedactionManifest
    """Populated redaction manifest (legal-hold mode while OQ-01 unresolved)."""


def _hmac_subject(pepper: bytes, subject_id: str) -> str:
    """Return ``sha256:<hex>`` HMAC of ``subject_id`` keyed by ``pepper``.

    The pepper MUST NOT be logged, stored in config files, or embedded in capsules.
    """
    mac = hmac.new(pepper, subject_id.encode("utf-8"), hashlib.sha256)
    return "sha256:" + mac.hexdigest()


def _redact_text(
    text: str,
    spans: list[PIISpan],
    pepper: bytes,
    subject_id: str,
) -> tuple[str, list[tuple[PIISpan, str]]]:
    """Replace PII spans in *text* with ``[REDACTED:<hmac>:<type>]`` tokens.

    Returns the redacted text and a list of ``(span, replacement)`` pairs.
    Spans are applied right-to-left to preserve indices.
    """
    subject_hmac = _hmac_subject(pepper, subject_id)
    replacements: list[tuple[PIISpan, str]] = []
    result = text
    # Sort spans right-to-left so earlier replacements don't shift later indices.
    for span in sorted(spans, key=lambda s: s.start, reverse=True):
        token = f"[REDACTED:{subject_hmac}:{span.entity_type}]"
        result = result[: span.start] + token + result[span.end :]
        replacements.append((span, token))
    return result, replacements


def _walk_and_redact(
    obj: Any,
    field_path: str,
    detector: PIIDetector,
    pepper: bytes,
    subject_id: str,
    manifest: RedactionManifest,
    legal_basis: str,
) -> tuple[Any, RedactionManifest, int]:
    """Recursively walk *obj*, redacting PII in string leaves.

    Returns ``(redacted_obj, updated_manifest, redaction_count)``.
    """
    redacted_count = 0

    if isinstance(obj, str):
        spans = detector.detect(obj)
        if not spans:
            return obj, manifest, 0
        redacted_text, replacements = _redact_text(obj, spans, pepper, subject_id)
        redacted_count += len(replacements)
        for span, _ in replacements:
            entry = RedactionManifestEntry(
                field_path=field_path,
                detection_rule_id=span.entity_type,
                legal_basis=legal_basis,
                subject_id_hmac=_hmac_subject(pepper, subject_id),
                redacted_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            manifest = manifest.append(entry)
        return redacted_text, manifest, redacted_count

    if isinstance(obj, dict):
        new_obj = {}
        for k, v in obj.items():
            child_path = f"{field_path}.{k}" if field_path else k
            new_v, manifest, count = _walk_and_redact(
                v, child_path, detector, pepper, subject_id, manifest, legal_basis
            )
            new_obj[k] = new_v
            redacted_count += count
        return new_obj, manifest, redacted_count

    if isinstance(obj, list):
        new_list = []
        for i, item in enumerate(obj):
            child_path = f"{field_path}[{i}]"
            new_item, manifest, count = _walk_and_redact(
                item, child_path, detector, pepper, subject_id, manifest, legal_basis
            )
            new_list.append(new_item)
            redacted_count += count
        return new_list, manifest, redacted_count

    # Scalar non-string — pass through.
    return obj, manifest, 0


class PIIDetectionGate:
    """PII detection and redaction gate for capsule payloads (cap-001).

    ACTIVE mode (OQ-01 resolved by ADR-0069, 2026-05-27):
      - Redacted manifests are produced and can be sealed into capsules.
      - Legal-hold staging to ``legal_hold/`` is disabled; manifests are live.

    Security:
      - The HMAC pepper is read from ``NOVA_PII_PEPPER`` env var only.
      - On any detection exception: ``PIIScanError`` is raised (fail-closed).
      - Errors are never silently swallowed.
    """

    def __init__(
        self,
        detector: PIIDetector,
        pepper: bytes | None = None,
        field_paths: list[str] | None = None,
        legal_basis: str = "operator_declared_compliance",
        legal_hold_dir: Path | None = None,
    ) -> None:
        """
        Args:
            detector: :class:`PIIDetector` to use.
            pepper: HMAC pepper bytes.  If ``None``, read from ``NOVA_PII_PEPPER``
                    env var.  **Never pass the pepper through config files or capsules.**
            field_paths: Dot-notation field paths to scan.  If ``None``, scan all
                         string leaves in the payload.
            legal_basis: Legal basis string recorded in the manifest.
            legal_hold_dir: Directory for legal-hold staging files.
                            Defaults to ``$NOVAFABRIC_HOME/legal_hold/`` or
                            ``~/.novafabric/legal_hold/``.
        """
        if pepper is None:
            raw = os.environ.get("NOVA_PII_PEPPER", "")
            if not raw:
                raise ValueError(
                    "NOVA_PII_PEPPER env var is not set.  "
                    "Set it to a random secret value before using PIIDetectionGate.  "
                    "The pepper must not appear in config files or capsules."
                )
            pepper = raw.encode("utf-8")

        self._detector = detector
        self._pepper = pepper
        self._field_paths = field_paths
        self._legal_basis = legal_basis

        if legal_hold_dir is None:
            home = Path(os.environ.get("NOVAFABRIC_HOME", Path.home() / ".novafabric"))
            legal_hold_dir = home / "legal_hold"
        self._legal_hold_dir = legal_hold_dir

    def scan(
        self,
        payload: dict[str, Any],
        capsule_id: str,
        subject_id: str = "unknown",
    ) -> RedactedPayload:
        """Scan *payload* for PII and return a :class:`RedactedPayload`.

        Emits structured log events:
          - ``pii_scan_completed`` always (when active)
          - ``pii_scan_failed`` on exception (then raises :class:`PIIScanError`)

        In active mode (OQ-01 resolved) the manifest is available for sealing.
        Legal-hold staging is a no-op (``_OQ01_UNRESOLVED = False``).

        Args:
            payload: The capsule payload dict to scan.
            capsule_id: ULID of the owning capsule (for manifest and staging).
            subject_id: Data-subject identifier.  The actual value is NEVER stored;
                        only its HMAC is recorded.

        Raises:
            PIIScanError: On any scanner error (fail-closed; never swallowed).
        """
        manifest = RedactionManifest(
            capsule_id=capsule_id,
            entries=[],
            legal_hold_mode=_OQ01_UNRESOLVED,
        )

        try:
            redacted_payload, manifest, redacted_count = _walk_and_redact(
                payload,
                "",
                self._detector,
                self._pepper,
                subject_id,
                manifest,
                self._legal_basis,
            )

            scanned_fields = list(payload.keys())

            logger.info(
                "pii_scan_completed",
                extra={
                    "event": "pii_scan_completed",
                    "capsule_id": capsule_id,
                    "redacted_count": redacted_count,
                    "legal_hold_mode": _OQ01_UNRESOLVED,
                },
            )

            result = RedactedPayload(
                original_fields=scanned_fields,
                redacted_count=redacted_count,
                payload=redacted_payload,
                manifest=manifest,
            )

            # Write manifest to legal_hold staging (OQ-01 unresolved).
            if _OQ01_UNRESOLVED and manifest.entries:
                self._write_legal_hold(capsule_id, manifest)

            return result

        except PIIScanError:
            raise
        except Exception as exc:
            logger.error(
                "pii_scan_failed",
                extra={
                    "event": "pii_scan_failed",
                    "capsule_id": capsule_id,
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise PIIScanError(
                f"PII scan failed for capsule {capsule_id}: {exc}"
            ) from exc

    def _write_legal_hold(self, capsule_id: str, manifest: RedactionManifest) -> None:
        """Write the manifest to legal_hold staging."""
        staging_dir = self._legal_hold_dir / capsule_id
        staging_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = staging_dir / "redaction-manifest.json"
        manifest_path.write_text(manifest.model_dump_json(indent=2))
        logger.info(
            "pii_legal_hold_staged",
            extra={
                "event": "pii_legal_hold_staged",
                "capsule_id": capsule_id,
                "manifest_path": str(manifest_path),
                "entry_count": len(manifest.entries),
            },
        )
