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
"""Synchronous NovaSeal signing wrapper for the Object Capsule Store.

cap-002 (FR-06, FR-08):
- Wraps the existing ``src/novafabric/trust/novaseal/`` module (``NovaSeal``).
- Does NOT reimplement NovaSeal core.
- ``sign(sha256_hex)`` calls ``NovaSeal.seal()`` on a single-field manifest
  dict ``{"capsule_sha256": sha256_hex}``.
- Raises ``NovaSealUnavailable`` on transient failures, triggering WAL fallback.
- Idempotent retry: if sign() is called again with the same sha256, the
  upstream NovaSeal Merkle log will append a new leaf — the caller is
  responsible for idempotency at the chain-commit level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from novafabric.object_capsule_store.exceptions import NovaSealUnavailable

log = logging.getLogger(__name__)


@dataclass
class NovaSealLeaf:
    """Result of a successful synchronous NovaSeal sign operation."""

    leaf_hash: str         # SHA-256 hex of the Merkle log leaf entry
    signed_at: str         # ISO-8601 UTC
    dsse_envelope_bytes: bytes


class NovaSealClient:
    """Thin synchronous wrapper around ``novafabric.trust.novaseal.NovaSeal``.

    Args:
        novaseal: An initialised ``NovaSeal`` instance from
                  ``src/novafabric/trust/novaseal/``.
    """

    def __init__(self, novaseal: object) -> None:
        # Lazy import to avoid hard dependency when NovaSeal is not configured.
        self._novaseal = novaseal

    def sign(self, capsule_sha256: str) -> NovaSealLeaf:
        """Sign *capsule_sha256* and return the Merkle-log leaf.

        Args:
            capsule_sha256: 64-char hex SHA-256 of the capsule canonical bytes.

        Returns:
            ``NovaSealLeaf`` with ``leaf_hash``, ``signed_at``, and
            ``dsse_envelope_bytes``.

        Raises:
            NovaSealUnavailable: on any signing failure (transient or permanent).
        """
        import hashlib
        import json

        manifest = {"capsule_sha256": capsule_sha256}
        try:
            bundle = self._novaseal.seal(manifest)  # type: ignore[attr-defined]
        except Exception as exc:
            raise NovaSealUnavailable(cause=str(exc)) from exc

        # Compute leaf_hash as SHA-256 of the serialised log_entry dict
        log_entry_bytes = json.dumps(
            bundle.log_entry, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        leaf_hash = hashlib.sha256(log_entry_bytes).hexdigest()

        signed_at = datetime.now(tz=timezone.utc).isoformat()
        return NovaSealLeaf(
            leaf_hash=leaf_hash,
            signed_at=signed_at,
            dsse_envelope_bytes=bundle.dsse_envelope,
        )

    def sign_bytes(self, data: bytes) -> bytes:
        """Convenience: serialise *data* bytes as a NovaSeal DSSE envelope.

        Used to create the ``novaseal.json`` sibling object.

        Returns:
            JSON bytes of the NovaSeal sibling payload.
        """
        import json

        sha256_hex = __import__("hashlib").sha256(data).hexdigest()
        leaf = self.sign(sha256_hex)
        payload = {
            "capsule_sha256": sha256_hex,
            "leaf_hash": leaf.leaf_hash,
            "signed_at": leaf.signed_at,
            "dsse_envelope": leaf.dsse_envelope_bytes.decode("utf-8"),
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class NovaSealDisabledClient:
    """NovaSeal stub that always raises ``NovaSealUnavailable``.

    Used in test fixtures that exercise the WAL fallback path.
    """

    def sign(self, capsule_sha256: str) -> NovaSealLeaf:
        raise NovaSealUnavailable("NovaSealDisabledClient — signing deliberately disabled")

    def sign_bytes(self, data: bytes) -> bytes:
        raise NovaSealUnavailable("NovaSealDisabledClient — signing deliberately disabled")
