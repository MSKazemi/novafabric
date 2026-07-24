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
from pathlib import Path

_CHUNK = 65536
_PREFIX = "sha256:"


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
