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

_V2_LEAF_PREFIX = b"\x02"


def _leaf_v2(rel_path: str, data: bytes) -> bytes:
    """Leaf committing to the file's capsule-relative path *and* its content.

    ``0x02 || len(path) (4 bytes, big-endian) || path (UTF-8, POSIX) || sha256(content)``.
    The length prefix makes the path/content boundary unambiguous. The ``0x02`` domain
    byte keeps v2 leaves apart from v1 leaves (``0x00``) and inner nodes (``0x01``):
    with a shared prefix, a file whose *content* is a v2 leaf preimage gives a v1 leaf
    equal to that v2 leaf, so a v1 root over crafted files could equal a v2 root.
    """
    path = rel_path.encode("utf-8")
    return _h(_V2_LEAF_PREFIX + len(path).to_bytes(4, "big") + path + _h(data))


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


def root_over_files(files: Iterable[tuple[str, bytes]], *, version: int = 2) -> str:
    """:func:`capsule_root` over ``(capsule-relative POSIX path, content)`` pairs.

    For a capsule that is not on disk, such as the ``run-capsule/`` copy inside an
    Evidence Bundle ZIP. Files are ordered by path components, which is the order
    ``sorted(capsule_dir.rglob("*"))`` gives, so both functions agree on one tree.
    """
    ordered = sorted(files, key=lambda item: item[0].split("/"))
    if version == 2:
        leaves = [_leaf_v2(rel, data) for rel, data in ordered]
    elif version == 1:
        leaves = [_leaf(data) for _, data in ordered]
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

    Accepting v1 does not let a v2-recorded root be satisfied by other files: v1 and v2
    leaves and inner nodes are hashed under distinct domain bytes, so a v1 root equals a
    v2 root only through a SHA-256 collision.
    """
    return any(
        expected == capsule_root(capsule_dir, version=v, exclude=exclude) for v in ROOT_VERSIONS
    )
