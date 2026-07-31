"""Dual-object GDPR/WORM capsule split: compliance-sealed + PII payload.

Disabled by default (NOVA_CAP003_ENABLED defaults to false). cap-003's own OQ-01
(does a BLAKE3 hash of erased PII constitute GDPR Art.17 erasure?) has never had a
EU-GDPR legal-counsel review — ADR-0066 and the CTO acceptance record (SCALE-ADR-003,
2026-05-17) both make `false` a mandatory safety gate until that review completes.
ADR-0069's "OQ-01 resolved" citation is for a *different* capability (cap-001's
AES-256-GCM DEK crypto-shredding) and, if anything, argues against this one: it
explicitly rejects hash-based tombstones per EDPB Guidelines 01/2025 ("a hash of
personal data is itself personal data"). Corrected 2026-07-30 — this module previously
defaulted to `true` on that mis-citation. cap-003 / ADR-0066 / ADR-0062.

S3 backend:
    When NOVA_S3_ENDPOINT_URL or NOVA_S3_BUCKET is set, ``split_and_store``
    writes to S3 via ``NovaObjectStore``.  Two buckets are supported:

    - ``NOVA_S3_COMPLIANCE_BUCKET`` — WORM/compliance (audit records)
    - ``NOVA_S3_GOVERNANCE_BUCKET`` — mutable/erasable (PII payload)

    If only ``NOVA_S3_BUCKET`` is set both objects go to that single bucket.
    The local-filesystem path (``split_and_store_local``) is always available
    for testing without S3.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novafabric.storage.nova_object_store import NovaObjectStore


@dataclass
class DualObjectResult:
    """Result from a dual-object store split operation (cap-003, ADR-0066)."""

    run_id: str
    audit_object_key: str  # path or key of the audit (no-PII) record
    pii_object_key: str | None  # path or key of PII payload; None when cap-003 disabled
    pii_content_digest: str | None  # BLAKE3 or SHA-256 hex of PII payload
    cap003_enabled: bool


class DualObjectStore:
    """Splits capsule data into compliance-sealed + PII payload objects.

    Disabled by default (NOVA_CAP003_ENABLED defaults to false — cap-003's own OQ-01,
    the BLAKE3-hash-as-erasure question, awaits EU-GDPR legal-counsel review; see the
    module docstring). Set NOVA_CAP003_ENABLED=true only after that review completes.

    When NOVA_S3_ENDPOINT_URL or NOVA_S3_BUCKET is set, ``split_and_store``
    writes to S3 via ``NovaObjectStore``; otherwise falls back to the local
    filesystem (same as ``split_and_store_local``).

    cap-003 / ADR-0066.
    """

    #: Fields treated as PII and redacted from the audit record.
    PII_KEYS: frozenset[str] = frozenset(
        {"output_text", "arguments", "context_snapshot", "prompt_text"}
    )

    def __init__(self) -> None:
        self.enabled = os.getenv("NOVA_CAP003_ENABLED", "false").lower() == "true"
        if self.enabled:
            logging.getLogger(__name__).warning(
                "cap-003 dual-object GDPR/WORM split ENABLED — this bypasses the "
                "mandatory false-until-legal-review safety gate (SCALE-ADR-003); "
                "OQ-01 has not had a EU-GDPR legal-counsel review"
            )

    @staticmethod
    def _compute_digest(data: bytes) -> str:
        """Compute SHA-256 digest (BLAKE3 preferred but optional dep)."""
        try:
            import blake3  # type: ignore[import-not-found,unused-ignore]

            return str(blake3.blake3(data).hexdigest())
        except ImportError:
            return hashlib.sha256(data).hexdigest()

    @classmethod
    def _redact(cls, obj: object) -> object:
        """Recursively replace PII field values with '[REDACTED]'."""
        if isinstance(obj, dict):
            return {
                k: "[REDACTED]" if k in cls.PII_KEYS else cls._redact(v)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [cls._redact(i) for i in obj]
        return obj

    @classmethod
    def _extract_pii_fields(
        cls, capsule_data: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Split capsule into audit (no PII) and PII payload.

        Returns (audit_record, pii_payload).  Top-level PII keys are replaced
        with "[REDACTED]" in the audit record; nested PII keys are recursively
        redacted via _redact().
        """
        audit_record: dict[str, object] = {
            k: "[REDACTED]" if k in cls.PII_KEYS else cls._redact(v)
            for k, v in capsule_data.items()
        }
        pii_payload: dict[str, object] = {
            k: v for k, v in capsule_data.items() if k in cls.PII_KEYS
        }
        return audit_record, pii_payload

    # ------------------------------------------------------------------
    # Backend selection
    # ------------------------------------------------------------------

    @staticmethod
    def _get_s3_backends() -> tuple["NovaObjectStore | None", "NovaObjectStore | None"]:
        """Return (compliance_backend, governance_backend) or (None, None).

        Returns ``None`` for both when S3 is not configured.  Each bucket is
        constructed lazily — import errors from missing boto3 are propagated.

        S3 is considered configured when *either* ``NOVA_S3_ENDPOINT_URL`` or
        ``NOVA_S3_BUCKET`` is set in the environment.
        """
        endpoint = os.getenv("NOVA_S3_ENDPOINT_URL")
        bucket = os.getenv("NOVA_S3_BUCKET")

        if not endpoint and not bucket:
            return None, None

        from novafabric.storage.nova_object_store import NovaObjectStore

        compliance_bucket = (
            os.getenv("NOVA_S3_COMPLIANCE_BUCKET")
            or bucket
            or "nova-capsules-compliance"
        )
        governance_bucket = (
            os.getenv("NOVA_S3_GOVERNANCE_BUCKET")
            or bucket
            or "nova-capsules-governance"
        )

        compliance_store = NovaObjectStore(
            endpoint_url=endpoint or None,
            bucket=compliance_bucket,
        )
        governance_store = NovaObjectStore(
            endpoint_url=endpoint or None,
            bucket=governance_bucket,
        )
        return compliance_store, governance_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def split_and_store(
        self,
        run_id: str,
        capsule_data: dict[str, object],
        output_dir: Path | None = None,
    ) -> DualObjectResult:
        """Split capsule and write to S3 (when configured) or local filesystem.

        Prefers S3 when ``NOVA_S3_ENDPOINT_URL`` or ``NOVA_S3_BUCKET`` is set.
        Falls back to ``output_dir`` (local filesystem) when S3 is not
        configured.  ``output_dir`` must be provided when S3 is not available.
        """
        log = logging.getLogger(__name__)
        compliance_store, governance_store = self._get_s3_backends()

        if compliance_store is None or governance_store is None:
            if output_dir is None:
                raise ValueError(
                    "output_dir is required when S3 is not configured "
                    "(NOVA_S3_ENDPOINT_URL / NOVA_S3_BUCKET not set)"
                )
            return self.split_and_store_local(run_id, capsule_data, output_dir)

        # S3 path
        audit_record, pii_payload = self._extract_pii_fields(capsule_data)

        audit_key = f"audit/{run_id}/audit.json"
        audit_bytes = json.dumps(audit_record, indent=2).encode()
        compliance_store.put_object(audit_key, audit_bytes, metadata={"run_id": run_id})
        log.debug("cap-003: wrote audit record to S3 key %s", audit_key)

        pii_bytes = json.dumps(pii_payload, indent=2).encode()
        digest = self._compute_digest(pii_bytes)

        pii_key: str | None = None
        pii_digest: str | None = None

        if self.enabled:
            pii_key = f"pii/{run_id}/pii.json"
            governance_store.put_object(
                pii_key,
                pii_bytes,
                metadata={"run_id": run_id, "digest": digest},
            )
            pii_digest = digest
            log.debug("cap-003: wrote PII payload to S3 key %s", pii_key)

        return DualObjectResult(
            run_id=run_id,
            audit_object_key=audit_key,
            pii_object_key=pii_key,
            pii_content_digest=pii_digest,
            cap003_enabled=self.enabled,
        )

    def split_and_store_local(
        self,
        run_id: str,
        capsule_data: dict[str, object],
        output_dir: Path,
    ) -> DualObjectResult:
        """Local filesystem implementation for testing without S3.

        Always writes the audit record.  Writes the PII payload only when
        cap-003 is enabled, and populates the digest accordingly.
        """
        audit_record, pii_payload = self._extract_pii_fields(capsule_data)

        audit_path = output_dir / f"{run_id}_audit.json"
        pii_path = output_dir / f"{run_id}_pii.json"

        audit_path.write_text(json.dumps(audit_record, indent=2), encoding="utf-8")

        pii_bytes = json.dumps(pii_payload, indent=2).encode()
        pii_path.write_bytes(pii_bytes)
        digest = self._compute_digest(pii_bytes)

        return DualObjectResult(
            run_id=run_id,
            audit_object_key=str(audit_path),
            pii_object_key=str(pii_path) if self.enabled else None,
            pii_content_digest=digest if self.enabled else None,
            cap003_enabled=self.enabled,
        )
