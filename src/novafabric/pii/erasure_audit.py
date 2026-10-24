"""One hash-only ``erasure.request`` audit entry per erasure, from every surface (ADR-0210).

``nova pii erase`` always wrote one; ``POST /api/compliance/pii/erase`` in
``nova serve`` wrote none, so an Art.17 erasure through the dashboard left no
record in the hash-chained log (review serve-04). Both now call
:func:`audit_erasure`. The subject id itself never enters the log: only its
SHA-256, and a digest of the receipt.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novafabric.pii.dek.store import (
        ErasureDeferredReceipt,
        ErasureHeldReceipt,
        ErasureReceipt,
    )


def audit_erasure(
    subject_id: str,
    result: ErasureReceipt | ErasureDeferredReceipt | ErasureHeldReceipt,
    *,
    actor: str,
    via: str,
) -> None:
    """Append the ``erasure.request`` entry for one erasure outcome.

    *via* names the surface (``nova pii erase``, ``POST /api/compliance/pii/erase``);
    *actor* is who asked (``cli-user``, or a serve token fingerprint).

    Raises:
        Exception: whatever :meth:`novafabric.audit.AuditLog.append` raises; the
            caller decides how to report a record that could not be written.
    """
    from novafabric.audit import AuditEventType, AuditLog, resolve_audit_log_path
    from novafabric.pii.dek.store import ErasureDeferredReceipt, ErasureReceipt

    extra: dict[str, object]
    if isinstance(result, ErasureReceipt):
        state, extra = "COMPLETED", {"capsule_ids": list(result.capsule_ids_affected)}
    elif isinstance(result, ErasureDeferredReceipt):
        state, extra = "DEFERRED", {"earliest_erasure_at": result.earliest_erasure_at.isoformat()}
    else:
        state, extra = "HELD", {
            "hold_ids": [h.hold_id for h in result.holds],
            "held_capsule_ids": [c.capsule_id for c in result.held_capsules],
            "dek_retained": True,
        }
    subject_hash = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()
    AuditLog(resolve_audit_log_path()).append(
        event_type=AuditEventType.ERASURE_REQUEST,
        actor=actor,
        resource_id=f"sha256:{subject_hash[:12]}",
        details={
            "via": via,
            "subject_sha256": subject_hash,
            "state": state,
            "receipt_sha256": hashlib.sha256(
                result.model_dump_json().encode("utf-8")
            ).hexdigest(),
            **extra,
        },
    )
