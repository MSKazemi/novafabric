"""RFC 6962-shaped Merkle root over a Run Capsule's files (ADR-0218, ADR-0310).

This is the *evidence* construction. ``trust/novaseal/merkle.py`` is a different,
incompatible one (ADR-0218); never mix leaves or combiners across the two.

The capsule is walked through :mod:`novafabric._capsule_fs`, the no-follow layer every
capsule exporter shares (ADR-0011 Amendments 3–5):

* a symbolic link anywhere in the capsule raises
  :class:`~novafabric._capsule_fs.CapsuleLinkError` naming every link. Following
  one would make the root commit to a file *outside* the capsule (``inputs/x ->
  ~/.ssh/id_rsa``), and that root is the signed subject of bundles, consent
  attestations, AI-BOMs and baseline pins. No root is ever computed over the
  remaining files;
* FIFOs, sockets and devices are not capsule content: they are listed without
  being opened and left out, as every exporter leaves them out and as the root
  always left them out. A listed regular file that is swapped for one before it is
  read raises :class:`~novafabric._capsule_fs.CapsuleSpecialFileError`, so the root
  never blocks on a FIFO;
* each file is opened with ``O_NOFOLLOW`` on every path component and read in
  bounded chunks, so a link planted after the listing is refused, not followed.

For a capsule without links the root is byte-identical to the one the earlier
``rglob`` walk produced, for both leaf versions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Final

from novafabric._capsule_fs import CapsuleLinkError, list_capsule, open_capsule_file

__all__ = [
    "ROOT_VERSIONS",
    "CapsuleLinkError",
    "capsule_merkle_root",
    "capsule_root",
    "capsule_root_matches",
    "root_over_files",
]

#: The ``operation`` named in a ``_capsule_fs`` refusal.
_OPERATION: Final[str] = "the capsule Merkle root"

#: Read size when hashing one capsule file; a file is never held in memory whole.
_CHUNK: Final[int] = 1 << 20


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


def _capsule_files(capsule_dir: Path) -> list[str]:
    """Every regular file's capsule-relative POSIX path, in root order.

    Ordered by path components, which is the order ``sorted(capsule_dir.rglob("*"))``
    gave and :func:`root_over_files` uses. Raises
    :class:`~novafabric._capsule_fs.CapsuleLinkError` when the capsule holds any link.
    """
    listing = list_capsule(capsule_dir, operation=_OPERATION)
    return sorted(listing.files, key=lambda rel: rel.split("/"))


def _content_digest(capsule_dir: Path, rel: str, prefix: bytes) -> bytes:
    """``sha256(prefix || content)`` of capsule file *rel*, streamed, following no link."""
    digest = hashlib.sha256(prefix)
    with open_capsule_file(capsule_dir, rel, operation=_OPERATION) as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.digest()


def _file_leaf(capsule_dir: Path, rel: str, version: int) -> bytes:
    """The *version* leaf of capsule file *rel* (same bytes as :func:`_leaf` / :func:`_leaf_v2`)."""
    if version == 2:
        return _leaf_v2_from_digest(rel, _content_digest(capsule_dir, rel, b""))
    return _content_digest(capsule_dir, rel, b"\x00")


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
    return _leaf_v2_from_digest(rel_path, _h(data))


def _leaf_v2_from_digest(rel_path: str, content_digest: bytes) -> bytes:
    """:func:`_leaf_v2` given ``sha256(content)`` instead of the content itself."""
    path = rel_path.encode("utf-8")
    return _h(_V2_LEAF_PREFIX + len(path).to_bytes(4, "big") + path + content_digest)


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

    ``exclude`` drops files (e.g. annotation streams) before hashing; it is called
    with ``capsule_dir / <relative path>``.

    The capsule is walked without following any link and each file is opened with
    ``O_NOFOLLOW`` and read in bounded chunks. FIFOs, sockets and devices are left
    out without being opened, as they always were.

    Raises:
        CapsuleLinkError: the capsule holds a symbolic link (never followed; no root
            is computed over the other files).
        CapsuleSpecialFileError: a listed file was swapped for a FIFO or device.
        NotADirectoryError: *capsule_dir* is not a directory.
        ValueError: *version* is unknown.
    """
    if version not in ROOT_VERSIONS:
        raise ValueError(f"unknown capsule root version {version!r}; known: {ROOT_VERSIONS}")
    capsule_dir = Path(capsule_dir)
    files = [
        rel
        for rel in _capsule_files(capsule_dir)
        if exclude is None or not exclude(capsule_dir / rel)
    ]
    leaves = [_file_leaf(capsule_dir, rel, version) for rel in files]
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
