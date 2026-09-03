"""Central SHA-256 helpers.

NovaFabric had ~150 ad-hoc ``hashlib.sha256`` call sites and two same-named but
behaviourally-different ``_sha256_bytes``/``_sha256_file`` helpers (one returned
``"sha256:"+hex`` and streamed the file in 64 KiB chunks; the other returned
bare hex and read the whole file into memory). This module is the single source
of truth so the prefix convention and streaming behaviour cannot drift again.

Two output conventions coexist deliberately:

* **bare hex** — used inside compliance exports and most digests.
* **prefixed** (``"sha256:"+hex``) — used by the Evidence Bundle manifest, which
  records algorithm-tagged digests.

Both file variants stream, so hashing a large file never loads it into memory.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_CHUNK = 65536
_PREFIX = "sha256:"

#: A canonical prefixed digest: ``sha256:`` + exactly 64 lowercase hex characters.
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class InvalidDigestError(ValueError):
    """A digest field did not hold a canonical ``sha256:<64 lowercase hex>`` value."""


def is_canonical_digest(value: object) -> bool:
    """True when *value* is a canonical prefixed SHA-256 digest string."""
    return isinstance(value, str) and DIGEST_RE.match(value) is not None


def validate_digest(value: object, *, field: str) -> str:
    """Return *value* if it is a canonical digest, else raise.

    Facet modules record digests instead of content, so a digest field is also a
    **containment boundary**: the wrong value there is not a formatting slip, it
    is raw payload reaching a record that promised not to hold any.

    Seven facet modules carry their own private ``_validate_digest``. Measured
    2026-09-02, all seven compile the identical pattern and **agree on accept and
    reject for every input** — what differs is the exception each raises and the
    guidance in its message, and that is deliberate: a science facet says
    "datasets, protocols, or manuscript bodies", a safety facet points at
    ``digest_payload()``, and each raises the error its own callers already catch.

    So this is not a consolidation target. It exists for **new** code that has no
    domain-specific digest exception of its own, so an eighth private copy does
    not have to be written.
    """
    if is_canonical_digest(value):
        return str(value)
    if isinstance(value, str) and value.lower().startswith(
        ("sha256:", "sha-256:", "sha256-")
    ):
        # It tried to be a digest: a typo, not a leak. Saying so plainly keeps an
        # honest mistake from reading as an exfiltration attempt.
        raise InvalidDigestError(
            f"{field} is not a canonical 'sha256:<64 lowercase hex>' digest: {value!r}"
        )
    raise InvalidDigestError(
        f"{field} must be a canonical 'sha256:<64 lowercase hex>' digest; "
        f"got {type(value).__name__}. Digest fields never hold content."
    )


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_prefixed(data: bytes) -> str:
    """Return ``"sha256:"`` + lowercase hex SHA-256 of ``data``."""
    return _PREFIX + hashlib.sha256(data).hexdigest()


def sha256_file_hex(path: Path) -> str:
    """Stream ``path`` and return its lowercase hex SHA-256."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file_prefixed(path: Path) -> str:
    """Stream ``path`` and return ``"sha256:"`` + its lowercase hex SHA-256."""
    return _PREFIX + sha256_file_hex(path)
