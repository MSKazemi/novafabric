"""The one helper that turns an untrusted id into a path (review server-01 and siblings).

Every id a request, manifest, event or ``capsule.yaml`` supplies passes through
:mod:`novafabric._safe_path` before it becomes a file or directory name. These
tests pin its rules; each call site has its own attack test as well.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from novafabric._safe_path import (
    MAX_SEGMENT_BYTES,
    PathEscapeError,
    UnsafePathSegmentError,
    atomic_write_bytes,
    is_safe_path_segment,
    require_path_segment,
    safe_child,
    write_private_file,
)

#: Values that must never become a path segment.
HOSTILE_IDS = [
    "/etc/passwd",
    "/",
    "..",
    ".",
    "../x",
    "../../escaped",
    "a/../../b",
    "a/b",
    "a\\b",
    "..\\x",
    "x\x00y",
    "x\ny",
    "x\x7fy",
    "a..b",
    "",
    "é" * 200,  # 400 UTF-8 bytes > NAME_MAX
]

CLEAN_IDS = ["01HXYZABCDEFGHJKMNPQRSTVWX", "run-a", "run_b.1", "%2e%2e", "a b"]


@pytest.mark.parametrize("value", HOSTILE_IDS)
def test_hostile_ids_are_refused(value: str) -> None:
    assert not is_safe_path_segment(value)
    with pytest.raises(UnsafePathSegmentError) as info:
        require_path_segment(value, "run_id")
    assert info.value.field == "run_id"
    assert "invalid run_id" in str(info.value)


@pytest.mark.parametrize("value", [None, 7, b"run", ["a"]])
def test_non_strings_are_refused(value: object) -> None:
    with pytest.raises(UnsafePathSegmentError):
        require_path_segment(value)


@pytest.mark.parametrize("value", CLEAN_IDS)
def test_clean_ids_pass(value: str) -> None:
    assert require_path_segment(value) == value


def test_longest_allowed_segment_passes() -> None:
    assert require_path_segment("a" * MAX_SEGMENT_BYTES)
    with pytest.raises(UnsafePathSegmentError):
        require_path_segment("a" * (MAX_SEGMENT_BYTES + 1))


@pytest.mark.parametrize("value", HOSTILE_IDS)
def test_safe_child_refuses_hostile_ids_and_creates_nothing(tmp_path: Path, value: str) -> None:
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(UnsafePathSegmentError):
        safe_child(base, value)
    assert list(tmp_path.iterdir()) == [base]


def test_safe_child_joins_a_clean_id(tmp_path: Path) -> None:
    assert safe_child(tmp_path, "run-a") == tmp_path / "run-a"
    assert safe_child(tmp_path, "run-a", suffix=".zip") == tmp_path / "run-a.zip"


def test_safe_child_refuses_a_suffix_that_breaks_the_segment(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathSegmentError):
        safe_child(tmp_path, "run", suffix="/../x")


def test_safe_child_refuses_a_symlinked_child(tmp_path: Path) -> None:
    """A link at the child name is never followed, even one pointing inside the base."""
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    (base / "run-a").symlink_to(outside, target_is_directory=True)
    (base / "inner").mkdir()
    (base / "alias").symlink_to(base / "inner", target_is_directory=True)
    (base / "dangling.zip").symlink_to(tmp_path / "nowhere")
    for name in ("run-a", "alias"):
        with pytest.raises(PathEscapeError):
            safe_child(base, name)
    with pytest.raises(PathEscapeError):
        safe_child(base, "dangling", suffix=".zip")


def test_safe_child_allows_a_base_reached_through_a_link(tmp_path: Path) -> None:
    """The base is the caller's own directory; only what the id names is distrusted."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert safe_child(link, "run-a") == link / "run-a"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_write_private_file_is_0600_even_under_a_permissive_umask(tmp_path: Path) -> None:
    old = os.umask(0o000)
    try:
        path = write_private_file(tmp_path / "keys" / "k.pem", b"secret")
    finally:
        os.umask(old)
    assert path.read_bytes() == b"secret"
    assert _mode(path) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["k.pem"], "no temp file left behind"


def test_write_private_file_replaces_a_planted_link_instead_of_writing_through(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    target = tmp_path / "k.pem"
    target.symlink_to(victim)
    write_private_file(target, b"secret")
    assert victim.read_bytes() == b"precious"
    assert not target.is_symlink()
    assert target.read_bytes() == b"secret"
    assert _mode(target) == 0o600


def test_write_private_file_breaks_a_hard_link(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    target = tmp_path / "k.pem"
    os.link(victim, target)
    write_private_file(target, b"secret")
    assert victim.read_bytes() == b"precious"


def test_exclusive_write_refuses_any_existing_entry(tmp_path: Path) -> None:
    existing = tmp_path / "k.pem"
    existing.write_bytes(b"first")
    with pytest.raises(FileExistsError):
        write_private_file(existing, b"second", exclusive=True)
    assert existing.read_bytes() == b"first"

    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    link = tmp_path / "link.pem"
    link.symlink_to(victim)
    with pytest.raises(FileExistsError):
        write_private_file(link, b"secret", exclusive=True)
    assert victim.read_bytes() == b"precious"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["k.pem", "link.pem", "victim.txt"]


def test_exclusive_write_creates_a_new_file(tmp_path: Path) -> None:
    path = write_private_file(tmp_path / "k.pem", b"secret", exclusive=True)
    assert path.read_bytes() == b"secret"
    assert _mode(path) == 0o600


def test_atomic_write_bytes_replaces_a_link_and_sets_the_mode(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    target = tmp_path / "out.zip"
    target.symlink_to(victim)
    atomic_write_bytes(target, b"PK")
    assert victim.read_bytes() == b"precious"
    assert target.read_bytes() == b"PK"
    assert _mode(target) == 0o644
