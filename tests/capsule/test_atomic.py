"""Tests for the fsync-durable atomic-commit helpers (enterprise-hardening 3.1)."""

from __future__ import annotations

from pathlib import Path

from novafabric.capsule._atomic import atomic_replace, fsync_dir, write_text_fsync


def test_write_text_fsync_writes_content(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    write_text_fsync(p, "hello")
    assert p.read_text() == "hello"


def test_atomic_replace_moves_file(tmp_path: Path) -> None:
    tmp = tmp_path / "f.tmp"
    dest = tmp_path / "f.json"
    write_text_fsync(tmp, '{"ok": true}')
    atomic_replace(tmp, dest)
    assert not tmp.exists()
    assert dest.read_text() == '{"ok": true}'


def test_atomic_replace_overwrites_existing(tmp_path: Path) -> None:
    dest = tmp_path / "f.json"
    dest.write_text("old")
    tmp = tmp_path / "f.tmp"
    write_text_fsync(tmp, "new")
    atomic_replace(tmp, dest)
    assert dest.read_text() == "new"


def test_atomic_replace_no_partial_dest_on_reader_view(tmp_path: Path) -> None:
    """dest either does not exist or has the full contents — never truncated."""
    dest = tmp_path / "f.json"
    tmp = tmp_path / "f.tmp"
    payload = "x" * 100_000
    write_text_fsync(tmp, payload)
    # Before replace, dest must not exist (no partial-write window).
    assert not dest.exists()
    atomic_replace(tmp, dest)
    assert dest.read_text() == payload


def test_fsync_dir_is_safe_on_directory(tmp_path: Path) -> None:
    # Should not raise on a normal directory.
    fsync_dir(tmp_path)


def test_fsync_dir_tolerates_missing_path(tmp_path: Path) -> None:
    # Best-effort: a non-existent directory must not raise.
    fsync_dir(tmp_path / "does-not-exist")
