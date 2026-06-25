"""Filesystem-backed store for promote proposal and approval bundles."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from novafabric.promote.bypass_notify import BypassEvent, BypassNotifier, NullBypassNotifier
from novafabric.promote.exceptions import BundleNotFoundError

logger = logging.getLogger(__name__)


class PromoteBundleStore:
    """Stores proposal and approval DSSE envelopes on the local filesystem.

    Directory layout::

        {base_dir}/promote/{capsule_id}/proposal/{uuid}.json
        {base_dir}/promote/{capsule_id}/approval/{proposal_uuid}.json
    """

    def __init__(self, base_dir: Path, notifier: BypassNotifier | None = None) -> None:
        self._base = base_dir
        self._notifier: BypassNotifier = notifier if notifier is not None else NullBypassNotifier()

    def _proposal_path(self, capsule_id: str, proposal_uuid: str) -> Path:
        return self._base / "promote" / capsule_id / "proposal" / f"{proposal_uuid}.json"

    def _approval_path(self, capsule_id: str, proposal_uuid: str) -> Path:
        return self._base / "promote" / capsule_id / "approval" / f"{proposal_uuid}.json"

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

    def put_bypass(self, capsule_id: str, envelope_bytes: bytes, valid_until: str) -> str:
        """Write a bypass envelope; return the generated UUID."""
        bypass_uuid = str(uuid.uuid4())
        path = self._bypass_dir(capsule_id) / f"{bypass_uuid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "envelope": envelope_bytes.decode("utf-8"),
            "bypass_uuid": bypass_uuid,
            "valid_until": valid_until,
        }
        path.write_text(json.dumps(doc), encoding="utf-8")
        try:
            self._notifier.notify(
                BypassEvent(
                    event_type="bypass_created",
                    capsule_id=capsule_id,
                    bypass_uuid=bypass_uuid,
                    valid_until=valid_until,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "bypass notify failed capsule=%r uuid=%r: %s", capsule_id, bypass_uuid, exc
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
