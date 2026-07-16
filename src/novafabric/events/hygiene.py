"""Payload hygiene: scan-before-emit secret redaction (ADR-0137 D5).

Every event record passes through the same secret-scanner rule pack the
capsule scanner uses before it leaves the process (log, webhook). A hit is
masked in place; the event is still emitted with the offending value
sanitized.
"""

from __future__ import annotations

import re
from typing import Any

from novafabric.capture.secrets import redact_secrets_in_text

# A labeled content digest ("sha256:<hex>") is a ref, not a credential —
# exempt it so digests are never mangled by hex-string credential rules.
_DIGEST_RE = re.compile(r"^(sha256|sha384|sha512|blake3):[0-9a-fA-F]+$")


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        if _DIGEST_RE.match(value):
            return value
        return redact_secrets_in_text(value)
    if isinstance(value, dict):
        return {k: _sanitize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    return value


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the event record with every string value secret-scanned."""
    return {k: _sanitize_value(v) for k, v in record.items()}
