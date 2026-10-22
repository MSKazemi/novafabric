"""The capsule Merkle root commits to file names (ADR-0310).

``evidence.merkle.capsule_merkle_root`` hashed file contents only, ordered by path,
so renaming a file without changing the sort order left the root — and every bundle
subject, consent attestation, AI-BOM and baseline pin built on it — unchanged.
The v2 leaf commits to the capsule-relative path as well. v1 roots recorded before
the change still verify through :func:`capsule_root_matches`, and accepting them does
not let a renamed capsule satisfy a v2 root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.capsule.comments import COMMENTS_FILENAME, capsule_subject_digest
from novafabric.evidence.merkle import (
    ROOT_VERSIONS,
    capsule_merkle_root,
    capsule_root,
    capsule_root_matches,
)


def _capsule(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


ORIGINAL = {"a.txt": b"one", "b.txt": b"two", "nested/tool-calls.jsonl": b"{}\n"}
RENAMED = {"a_renamed.txt": b"one", "b.txt": b"two", "nested/tool-calls.jsonl": b"{}\n"}


def test_renaming_a_file_changes_the_root(tmp_path: Path) -> None:
    original = _capsule(tmp_path / "o", ORIGINAL)
    renamed = _capsule(tmp_path / "r", RENAMED)
    assert capsule_merkle_root(original) != capsule_merkle_root(renamed)


def test_the_v1_construction_is_kept_and_is_rename_blind(tmp_path: Path) -> None:
    """Documents why v1 was replaced; v1 exists only to verify old roots."""
    original = _capsule(tmp_path / "o", ORIGINAL)
    renamed = _capsule(tmp_path / "r", RENAMED)
    assert capsule_merkle_root(original, version=1) == capsule_merkle_root(renamed, version=1)


def test_moving_a_file_to_another_directory_changes_the_root(tmp_path: Path) -> None:
    a = _capsule(tmp_path / "a", {"x/data.json": b"1", "y.txt": b"2"})
    b = _capsule(tmp_path / "b", {"w/data.json": b"1", "y.txt": b"2"})
    assert capsule_merkle_root(a) != capsule_merkle_root(b)


def test_a_root_recorded_under_either_version_still_verifies(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path / "c", ORIGINAL)
    for version in ROOT_VERSIONS:
        assert capsule_root_matches(capsule, capsule_merkle_root(capsule, version=version))


def test_a_renamed_capsule_cannot_satisfy_a_v2_root(tmp_path: Path) -> None:
    v2_root = capsule_merkle_root(_capsule(tmp_path / "o", ORIGINAL))
    assert not capsule_root_matches(_capsule(tmp_path / "r", RENAMED), v2_root)


def test_unknown_versions_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown capsule root version"):
        capsule_root(_capsule(tmp_path / "c", ORIGINAL), version=3)


def test_the_comment_subject_stays_on_v1(tmp_path: Path) -> None:
    """A stored identity label, not an integrity proof: moving it would orphan comments."""
    capsule = _capsule(tmp_path / "c", ORIGINAL)
    assert capsule_subject_digest(capsule) == capsule_merkle_root(capsule, version=1)
    (capsule / COMMENTS_FILENAME).write_text("{}\n")
    assert capsule_subject_digest(capsule) == capsule_merkle_root(
        _capsule(tmp_path / "o2", ORIGINAL), version=1
    )


def test_both_entry_points_default_to_v2(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path / "c", ORIGINAL)
    v2 = capsule_root(capsule, version=2)
    assert capsule_root(capsule) == capsule_merkle_root(capsule) == v2
    assert v2 != capsule_root(capsule, version=1)


def test_a_v1_root_over_crafted_files_cannot_equal_a_v2_root(tmp_path: Path) -> None:
    """With a shared 0x00 leaf prefix, files whose content is each v2 leaf's preimage
    gave a v1 root equal to the original's v2 root — satisfying it with none of its
    bytes. v2 leaves are domain-separated (0x02), so this is now a SHA-256 collision."""
    import hashlib

    original = _capsule(tmp_path / "o", ORIGINAL)
    crafted = {
        rel: len(rel.encode()).to_bytes(4, "big") + rel.encode() + hashlib.sha256(data).digest()
        for rel, data in ORIGINAL.items()
    }
    forged = _capsule(tmp_path / "f", crafted)
    v2_root = capsule_merkle_root(original)
    assert capsule_merkle_root(forged, version=1) != v2_root
    assert not capsule_root_matches(forged, v2_root)


@pytest.mark.parametrize("version", ROOT_VERSIONS)
def test_root_over_files_matches_the_on_disk_root(tmp_path: Path, version: int) -> None:
    """One order for both entry points, including names where '-' sorts before '/'."""
    from novafabric.evidence.merkle import root_over_files

    files = {"a/b.txt": b"1", "a-c.txt": b"2", "a.txt": b"3", "z/y/x": b"4", "B": b"5"}
    capsule = _capsule(tmp_path / "c", files)
    assert root_over_files(files.items(), version=version) == capsule_root(
        capsule, version=version
    )
