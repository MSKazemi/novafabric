from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path


def _h(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _leaf(data: bytes) -> bytes:
    return _h(b"\x00" + data)


def _inner(left: bytes, right: bytes) -> bytes:
    return _h(b"\x01" + left + right)


def _merkle_root(leaves: list[bytes]) -> bytes:
    if not leaves:
        return _h(b"")
    if len(leaves) == 1:
        return leaves[0]
    mid = 1
    while mid * 2 < len(leaves):
        mid *= 2
    left = _merkle_root(leaves[:mid])
    right = _merkle_root(leaves[mid:])
    return _inner(left, right)


def _iter_capsule_files(capsule_dir: Path) -> Iterable[Path]:
    for path in sorted(capsule_dir.rglob("*")):
        if path.is_file():
            yield path


def capsule_merkle_root(capsule_dir: Path) -> str:
    """RFC 6962-style SHA-256 Merkle root over all files in the capsule.

    Files are ordered by sorted relative path; leaves use 0x00 prefix, inner
    nodes use 0x01 prefix. Empty directory hashes to sha256("").
    """
    leaves = [_leaf(path.read_bytes()) for path in _iter_capsule_files(capsule_dir)]
    return "sha256:" + _merkle_root(leaves).hex()
