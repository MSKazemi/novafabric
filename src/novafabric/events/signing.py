"""Optional HMAC-SHA256 event signing (ADR-0137 D5; stdlib only).

The signature is computed over the canonical (sorted-key, compact, UTF-8)
JSON body *excluding* the ``signature`` field. NovaSeal detached signatures
(``alg: novaseal``) are future design (ADR-0041) and not implemented here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

HMAC_ALG = "hmac-sha256"
SIGNATURE_HEADER = "X-NovaFabric-Signature"


def canonical_body(record: dict[str, Any]) -> bytes:
    """Canonical signing body: sorted-key compact JSON, ``signature`` excluded."""
    body = {k: v for k, v in record.items() if k != "signature"}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_record(record: dict[str, Any], secret: bytes, keyid: str) -> dict[str, Any]:
    """Return a copy of ``record`` with an ``hmac-sha256`` signature attached."""
    value = hmac.new(secret, canonical_body(record), hashlib.sha256).hexdigest()
    signed = dict(record)
    signed["signature"] = {"alg": HMAC_ALG, "keyid": keyid, "value": value}
    return signed


def verify_record(record: dict[str, Any], secret: bytes) -> bool:
    """Verify an ``hmac-sha256``-signed record (receiver-side convenience)."""
    signature = record.get("signature")
    if not isinstance(signature, dict) or signature.get("alg") != HMAC_ALG:
        return False
    expected = hmac.new(secret, canonical_body(record), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature.get("value", "")))
