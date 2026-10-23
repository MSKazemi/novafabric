"""Filesystem-backed store for promote proposal and approval bundles."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path

from novafabric.promote.bypass_notify import (
    BypassEvent,
    BypassNotifier,
    build_notifier_from_env,
    channel_names,
    dispatch_bypass_event,
)
from novafabric.promote.exceptions import BundleNotFoundError, BypassAuditError

logger = logging.getLogger(__name__)


def _audit_bypass_created(
    *,
    capsule_id: str,
    bypass_uuid: str,
    valid_until: str,
    envelope_bytes: bytes,
    actor: str,
    surface: str,
    authorized_by: str | None,
    channels: list[str],
) -> None:
    """Append the ``seal.bypass.create`` entry; raise :class:`BypassAuditError` if it fails.

    The log path comes from :func:`novafabric.audit.resolve_audit_log_path` at call
    time, like every other writer of the hash-chained log.
    """
    from novafabric.audit import (  # noqa: PLC0415 — keep the store import-light
        AuditEventType,
        AuditLog,
        resolve_audit_log_path,
    )

    details: dict[str, object] = {
        "bypass_uuid": bypass_uuid,
        "valid_until": valid_until,
        "envelope_sha256": hashlib.sha256(envelope_bytes).hexdigest(),
        "notification_channels": channels,
        "surface": surface,
    }
    if authorized_by is not None:
        details["authorized_by"] = authorized_by
    try:
        AuditLog(resolve_audit_log_path()).append(
            event_type=AuditEventType.SEAL_BYPASS_CREATE,
            actor=actor,
            resource_id=capsule_id,
            details=details,
        )
    except Exception as exc:
        logger.error(
            "bypass refused: its audit entry could not be written: %s",
            type(exc).__name__,
            extra={"capsule_id": capsule_id, "bypass_uuid": bypass_uuid},
        )
        raise BypassAuditError(
            "bypass refused: the audit log could not be written "
            f"({type(exc).__name__}); nothing was stored"
        ) from exc


class PromoteBundleStore:
    """Stores proposal and approval DSSE envelopes on the local filesystem.

    Directory layout::

        {base_dir}/promote/{capsule_id}/proposal/{uuid}.json
        {base_dir}/promote/{capsule_id}/approval/{proposal_uuid}.json
    """

    def __init__(self, base_dir: Path, notifier: BypassNotifier | None = None) -> None:
        self._base = base_dir
        # Default: the channels NOVA_BYPASS_NOTIFY_FILE / _WEBHOOK configure (audit
        # H3 — the default was a no-op, so no bypass ever notified anyone).
        self._notifier: BypassNotifier = (
            notifier if notifier is not None else build_notifier_from_env()
        )

    def _proposal_path(self, capsule_id: str, proposal_uuid: str) -> Path:
        return self._base / "promote" / capsule_id / "proposal" / f"{proposal_uuid}.json"

    def _approval_path(self, capsule_id: str, proposal_uuid: str) -> Path:
        return self._base / "promote" / capsule_id / "approval" / f"{proposal_uuid}.json"

    def approval_path(self, capsule_id: str, proposal_uuid: str) -> Path:
        """Where :meth:`put_approval` stores the approval for *proposal_uuid*.

        Keyed by the proposal it approves, not by the approval's own UUID. For
        evidence that must point at the stored bundle (L21).
        """
        return self._approval_path(capsule_id, proposal_uuid)

    def bypass_path(self, capsule_id: str, bypass_uuid: str) -> Path:
        """Where :meth:`put_bypass` stores the bypass *bypass_uuid* (L21)."""
        return self._bypass_dir(capsule_id) / f"{bypass_uuid}.json"

    # ------------------------------------------------------------------
    # Proposal
    # ------------------------------------------------------------------

    def put_proposal(self, capsule_id: str, envelope_bytes: bytes) -> str:
        """Write a proposal envelope; return the generated UUID."""
        proposal_uuid = str(uuid.uuid4())
        path = self._proposal_path(capsule_id, proposal_uuid)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"envelope": envelope_bytes.decode("utf-8")}
        path.write_text(json.dumps(doc), encoding="utf-8")
        return proposal_uuid

    def get_proposal(self, capsule_id: str, proposal_uuid: str) -> bytes:
        """Read a proposal envelope; raises BundleNotFoundError."""
        path = self._proposal_path(capsule_id, proposal_uuid)
        if not path.exists():
            raise BundleNotFoundError(
                f"Proposal bundle not found: capsule={capsule_id!r} uuid={proposal_uuid!r}"
            )
        doc: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        return doc["envelope"].encode("utf-8")

    def list_proposals(self, capsule_id: str) -> list[str]:
        """Return sorted list of proposal UUIDs for *capsule_id*."""
        proposal_dir = self._base / "promote" / capsule_id / "proposal"
        if not proposal_dir.exists():
            return []
        return sorted(
            p.stem for p in proposal_dir.glob("*.json")
        )

    # ------------------------------------------------------------------
    # Approval
    # ------------------------------------------------------------------

    def put_approval(
        self,
        capsule_id: str,
        envelope_bytes: bytes,
        proposal_uuid: str,
    ) -> str:
        """Write an approval envelope linked to *proposal_uuid*; return the approval UUID."""
        approval_uuid = str(uuid.uuid4())
        path = self._approval_path(capsule_id, proposal_uuid)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "envelope": envelope_bytes.decode("utf-8"),
            "approval_uuid": approval_uuid,
        }
        path.write_text(json.dumps(doc), encoding="utf-8")
        return approval_uuid

    def get_approval(self, capsule_id: str, proposal_uuid: str) -> bytes:
        """Read approval envelope for *proposal_uuid*; raises BundleNotFoundError."""
        path = self._approval_path(capsule_id, proposal_uuid)
        if not path.exists():
            raise BundleNotFoundError(
                f"Approval bundle not found: capsule={capsule_id!r} proposal={proposal_uuid!r}"
            )
        doc: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        return doc["envelope"].encode("utf-8")

    # ------------------------------------------------------------------
    # Bypass
    # ------------------------------------------------------------------

    def _bypass_dir(self, capsule_id: str) -> Path:
        return self._base / "promote" / capsule_id / "bypass"

    def notification_channels(self) -> list[str]:
        """The bypass notification channels this store will deliver to.

        Callers sign this list into the bypass predicate (``notification_sent_to``)
        *before* :meth:`put_bypass` delivers, so the signature covers the
        configuration, never a claimed delivery. Webhooks appear as
        ``webhook:scheme://host[:port]`` (no path, query or credential).
        """
        return channel_names(self._notifier)

    def put_bypass(
        self,
        capsule_id: str,
        envelope_bytes: bytes,
        valid_until: str,
        *,
        actor: str = "unspecified",
        surface: str = "api",
        authorized_by: str | None = None,
        recipients: list[str] | None = None,
    ) -> str:
        """Store a bypass envelope, audit it, then notify; return the generated UUID.

        Order, and why:

        1. The bundle is written to a temporary file the store never lists.
        2. A ``seal.bypass.create`` entry is appended to the hash-chained audit log.
           If that fails, the temporary file is removed and :class:`BypassAuditError`
           is raised: a bypass skips the two-person rule, and the audit entry is its
           compensating control, so no entry means no bypass (fail closed). Nothing
           is stored and nothing is announced.
        3. The file is renamed into place (atomic on one filesystem).
        4. The configured channels are notified. A failing channel is logged and
           audited (``seal.bypass.notify``) but never blocks the bypass.
        """
        bypass_uuid = str(uuid.uuid4())
        path = self.bypass_path(capsule_id, bypass_uuid)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "envelope": envelope_bytes.decode("utf-8"),
            "bypass_uuid": bypass_uuid,
            "valid_until": valid_until,
        }
        staged = path.with_name(f".{bypass_uuid}.json.staged")
        staged.write_text(json.dumps(doc), encoding="utf-8")
        try:
            _audit_bypass_created(
                capsule_id=capsule_id,
                bypass_uuid=bypass_uuid,
                valid_until=valid_until,
                envelope_bytes=envelope_bytes,
                actor=actor,
                surface=surface,
                authorized_by=authorized_by,
                channels=self.notification_channels(),
            )
            os.replace(staged, path)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        dispatch_bypass_event(
            self._notifier,
            BypassEvent(
                event_type="bypass_created",
                capsule_id=capsule_id,
                bypass_uuid=bypass_uuid,
                valid_until=valid_until,
                requester_keyid=authorized_by,
                recipients=list(recipients or []),
            ),
        )
        return bypass_uuid

    def get_bypass(self, capsule_id: str, bypass_uuid: str) -> bytes:
        """Read bypass envelope; raises BundleNotFoundError."""
        path = self._bypass_dir(capsule_id) / f"{bypass_uuid}.json"
        if not path.exists():
            raise BundleNotFoundError(
                f"Bypass not found: capsule={capsule_id!r} uuid={bypass_uuid!r}"
            )
        doc: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        return doc["envelope"].encode("utf-8")

    def list_bypasses(self, capsule_id: str) -> list[str]:
        """Return sorted list of bypass UUIDs for *capsule_id*."""
        d = self._bypass_dir(capsule_id)
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    def get_bypass_valid_until(self, capsule_id: str, bypass_uuid: str) -> str:
        """Return valid_until ISO string; raises BundleNotFoundError."""
        path = self._bypass_dir(capsule_id) / f"{bypass_uuid}.json"
        if not path.exists():
            raise BundleNotFoundError(
                f"Bypass not found: capsule={capsule_id!r} uuid={bypass_uuid!r}"
            )
        doc: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        return doc["valid_until"]
