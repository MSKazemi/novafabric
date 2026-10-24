"""One hash-only ``erasure.request`` audit entry per erasure, from every surface (ADR-0210).

``nova pii erase`` always wrote one; ``POST /api/compliance/pii/erase`` in
``nova serve`` wrote none, so an Art.17 erasure through the dashboard left no
record in the hash-chained log (review serve-04). Both now call
:func:`audit_erasure`.

The subject never enters the log, and neither does a bare hash of it: subject ids
are emails and user ids, and ``sha256("alice@example.com")`` is reversed by a
dictionary in seconds — in a log that cannot be edited after the erasure it
records, and that is shipped to SIEM (review compliance-privacy-15, EDPB 01/2025).
The entry carries :func:`subject_audit_ref` instead: the HMAC keyed with
``NOVA_PII_PEPPER``, the same subject reference the redaction manifest uses, or
nothing when no pepper is configured.
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novafabric.pii.dek.store import (
        ErasureDeferredReceipt,
        ErasureHeldReceipt,
        ErasureReceipt,
    )


#: Environment variable holding the HMAC pepper (ADR-0314).
PEPPER_ENV = "NOVA_PII_PEPPER"


def subject_audit_ref(subject_id: str) -> str | None:
    """``sha256:<hex>`` HMAC of *subject_id* keyed with ``NOVA_PII_PEPPER``; ``None`` without it.

    Correlates with the capsule redaction manifest's subject reference for an
    operator who holds the pepper; reveals nothing to a reader who does not.
    """
    pepper = os.environ.get(PEPPER_ENV, "")
    if not pepper:
        return None
    from novafabric.pii.envelope import subject_ref

    return subject_ref(pepper.encode("utf-8"), subject_id)


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
    ref = subject_audit_ref(subject_id)
    receipt_sha256 = hashlib.sha256(result.model_dump_json().encode("utf-8")).hexdigest()
    AuditLog(resolve_audit_log_path()).append(
        event_type=AuditEventType.ERASURE_REQUEST,
        actor=actor,
        resource_id=ref[:19] if ref else f"erasure-receipt:sha256:{receipt_sha256[:12]}",
        details={
            "via": via,
            "subject_ref": ref,
            "state": state,
            "receipt_sha256": receipt_sha256,
            **extra,
        },
    )
