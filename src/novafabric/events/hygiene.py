"""Payload hygiene: scan-before-emit secret redaction (ADR-0137 D5).

Every event record passes through the same secret-scanner rule pack the
capsule scanner uses before it leaves the process (log, webhook). A hit is
masked in place; the event is still emitted with the offending value
sanitized.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from novafabric.capture.secrets import redact_secrets_in_text

# A labeled content digest ("sha256:<hex>") is a ref, not a credential —
# exempt it so digests are never mangled by hex-string credential rules.
_DIGEST_RE = re.compile(r"^(sha256|sha384|sha512|blake3):[0-9a-fA-F]+\Z")


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


def redact_url(url: str) -> str:
    """``scheme://host[:port]`` plus ``/…`` when there was more — never the path or query.

    A webhook URL is often the credential itself: a Slack incoming webhook is
    ``https://hooks.slack.com/services/T…/B…/<secret>``, and Discord and Teams
    URLs carry their token the same way (review misc-2-04). The host is kept so
    an operator can still tell which endpoint failed; userinfo is dropped too.
    """
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "<webhook url>"
    if not parts.scheme or not host:
        return "<webhook url>"
    shown = f"{parts.scheme}://{host}" + (f":{port}" if port else "")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        shown += "/…"
    return shown


def scrub_url(text: str, url: str) -> str:
    """*text* with every spelling of *url* replaced by :func:`redact_url`.

    HTTP client errors embed the request URL (httpx: ``… for url '<url>'``), and
    may re-encode it, so the path and query are replaced on their own as well.
    The result is also run through the secret scanner.
    """
    shown = redact_url(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    needles = [url]
    if parts is not None:
        tail = parts.path + (f"?{parts.query}" if parts.query else "")
        if len(tail) > 1:
            needles += [tail, parts.path] if parts.query else [tail]
    for needle in sorted(set(needles), key=len, reverse=True):
        if needle:
            text = text.replace(needle, shown)
    return redact_secrets_in_text(text)
