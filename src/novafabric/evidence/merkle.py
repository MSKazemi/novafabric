from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
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


#: Root constructions, newest first. Verifiers accept any of them (ADR-0310).
ROOT_VERSIONS = (2, 1)


def _leaf_v2(rel_path: str, data: bytes) -> bytes:
    """Leaf committing to the file's capsule-relative path *and* its content.

    ``0x00 || len(path) (4 bytes, big-endian) || path (UTF-8, POSIX) || sha256(content)``.
    The length prefix makes the path/content boundary unambiguous.
    """
    path = rel_path.encode("utf-8")
    return _h(b"\x00" + len(path).to_bytes(4, "big") + path + _h(data))


def capsule_root(
    capsule_dir: Path,
    *,
    version: int = 2,
    exclude: Callable[[Path], bool] | None = None,
) -> str:
    """SHA-256 Merkle root over the files of *capsule_dir* (RFC 6962 tree shape).

    Files are ordered by sorted relative path; inner nodes use the 0x01 prefix and an
    empty directory hashes to sha256(""). ``version`` picks the leaf:

    * **2** (default since ADR-0310) — each leaf commits to the file's relative path and
      content, so renaming a file changes the root;
    * **1** — content only (``0x00 || content``). Renaming a file without changing the
      sort order leaves a v1 root unchanged. Kept so roots recorded before ADR-0310
      still verify.

    ``exclude`` drops files (e.g. annotation streams) before hashing.
    """
    files = [p for p in _iter_capsule_files(capsule_dir) if exclude is None or not exclude(p)]
    if version == 2:
        leaves = [_leaf_v2(p.relative_to(capsule_dir).as_posix(), p.read_bytes()) for p in files]
    elif version == 1:
        leaves = [_leaf(p.read_bytes()) for p in files]
    else:
        raise ValueError(f"unknown capsule root version {version!r}; known: {ROOT_VERSIONS}")
    return "sha256:" + _merkle_root(leaves).hex()


def capsule_merkle_root(capsule_dir: Path, *, version: int = 2) -> str:
    """The capsule's Merkle root (:func:`capsule_root`; v2 unless *version* says otherwise)."""
    return capsule_root(capsule_dir, version=version)


def capsule_root_matches(
    capsule_dir: Path, expected: str, *, exclude: Callable[[Path], bool] | None = None
) -> bool:
    """True when *expected* is this capsule's root under any known construction.

    Accepting v1 does not let a v2-recorded root be satisfied by a renamed capsule:
    the recorded value is a v2 root, and a v1 root of other files cannot equal it.
    """
    return any(
        expected == capsule_root(capsule_dir, version=v, exclude=exclude) for v in ROOT_VERSIONS
    )
