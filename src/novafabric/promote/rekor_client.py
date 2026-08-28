"""Optional Rekor transparency log client.

Publish DSSE envelopes to a Rekor instance when NOVA_REKOR_URL is set.
If NOVA_REKOR_URL is not set, all functions are no-ops.
Network errors are logged as warnings and do NOT raise — Rekor is additive.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

# Third private copy of the base64 decoder, now consolidated: the payload hash
# published to Rekor is computed from it, so a decoder that drifts from the
# encoder writes a wrong hash into a transparency log.
from novafabric.trust.novaseal.envelope import _b64_decode as _decode_b64url

logger = logging.getLogger(__name__)

REKOR_ENTRY_KIND = "dsse"
REKOR_ENTRY_API_VERSION = "0.0.1"




def maybe_publish(envelope_bytes: bytes, *, timeout_s: float = 10.0) -> str | None:
    """Publish *envelope_bytes* to Rekor if NOVA_REKOR_URL is set.

    Returns the Rekor log entry UUID (opaque string) on success, None otherwise.
    Never raises — caller must handle None as "not published".
    """
    rekor_url = os.environ.get("NOVA_REKOR_URL")
    if not rekor_url:
        return None

    try:
        import httpx  # noqa: PLC0415

        envelope: dict[str, Any] = json.loads(envelope_bytes)
        payload_b64 = envelope.get("payload", "")
        payload_bytes = _decode_b64url(payload_b64)
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()

        # Rekor dsse entry format
        entry_body: dict[str, Any] = {
            "kind": REKOR_ENTRY_KIND,
            "apiVersion": REKOR_ENTRY_API_VERSION,
            "spec": {
                "signatures": envelope.get("signatures", []),
                "payloadHash": {
                    "algorithm": "sha256",
                    "value": payload_hash,
                },
            },
        }

        resp = httpx.post(
            f"{rekor_url.rstrip('/')}/api/v1/log/entries",
            json=entry_body,
            timeout=timeout_s,
        )
        if resp.status_code in (200, 201):
            data: dict[str, Any] = resp.json()
            if data:
                log_uuid = next(iter(data.keys()))
                logger.info("Rekor entry published: %s", log_uuid)
                return log_uuid
        logger.warning(
            "Rekor push rejected: HTTP %s — %s",
            resp.status_code,
            resp.text[:200],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rekor push error: %s", exc)
    return None
