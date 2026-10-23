"""A pinned capsule root is never swapped from under a writer; a hard link never carries a write.

``replace_capsule_file`` and ``open_capsule_file_for_write`` open the capsule *root*
by name, so a link the caller named (``runs/latest``) works. During capture
finalisation a process the workload left running could swap the capsule directory
for a link after the post-workload sweep, and every later write would have landed
wherever it pointed. ``pin_capsule_root`` closes that: under a pin the root is
opened ``O_NOFOLLOW`` and must still be the same ``(st_dev, st_ino)``.

``open_capsule_file_for_write`` used to append to (or truncate) a hard-linked file in
place, which wrote through to the file's other names -- possibly outside the capsule.
It now gives the file a private copy first.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from novafabric._capsule_fs import (
    CapsuleLinkError,
    CapsuleRootChangedError,
    capsule_root_pin_scope,
    make_capsule_dir,
    open_capsule_file,
    open_capsule_file_for_write,
    pin_capsule_root,
    read_capsule_file,
    replace_capsule_file,
    with_capsule_root_pins,
)

ORIGINAL = b"OUTSIDE-ORIGINAL\n"


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    path = tmp_path / "runs" / "01CAPSULE"
    (path / "outputs").mkdir(parents=True)
    (path / "trace.jsonl").write_bytes(b"")
    return path


def _identity(path: Path) -> tuple[int, int]:
    st = path.lstat()
    return st.st_dev, st.st_ino


def _swap_for_link(capsule: Path, tmp_path: Path) -> Path:
    """Move the capsule away and put a link to an empty outside directory in its place."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "outputs").mkdir(parents=True)
    capsule.rename(tmp_path / "moved-away")
    capsule.symlink_to(elsewhere, target_is_directory=True)
    return elsewhere


def _files(path: Path) -> list[str]:
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())


# ── root pin ─────────────────────────────────────────────────────────────────


def test_a_swapped_pinned_root_is_refused_by_every_helper(capsule: Path, tmp_path: Path) -> None:
    identity = _identity(capsule)
    elsewhere = _swap_for_link(capsule, tmp_path)
    with capsule_root_pin_scope():
        pin_capsule_root(capsule, identity)
        with pytest.raises(CapsuleRootChangedError):
            replace_capsule_file(capsule, "capsule.yaml", b"x")
        with pytest.raises(CapsuleRootChangedError):
            replace_capsule_file(capsule, "outputs/stdout.txt", b"x")
        with pytest.raises(CapsuleRootChangedError):
            open_capsule_file_for_write(capsule, "trace.jsonl", mode="a")
        with pytest.raises(CapsuleRootChangedError):
            make_capsule_dir(capsule, ".seal")
        with pytest.raises(CapsuleRootChangedError):
            read_capsule_file(capsule, "trace.jsonl")
        # A root below the pinned one is checked through the pin as well.
        with pytest.raises(CapsuleRootChangedError):
            replace_capsule_file(capsule / "outputs", "x.txt", b"x")
    assert _files(elsewhere) == []
    assert not (elsewhere / ".seal").exists()


def test_a_root_swapped_for_another_real_directory_is_refused(
    capsule: Path, tmp_path: Path
) -> None:
    identity = _identity(capsule)
    capsule.rename(tmp_path / "moved-away")
    capsule.mkdir()  # same path, different directory: only the identity can tell
    with capsule_root_pin_scope():
        pin_capsule_root(capsule, identity)
        with pytest.raises(CapsuleRootChangedError):
            replace_capsule_file(capsule, "capsule.yaml", b"x")
    assert list(capsule.iterdir()) == []


def test_an_unchanged_pinned_root_works(capsule: Path) -> None:
    with capsule_root_pin_scope():
        pin_capsule_root(capsule, _identity(capsule))
        replace_capsule_file(capsule, "outputs/stdout.txt", b"hello\n")
        with open_capsule_file_for_write(capsule, "trace.jsonl", mode="a") as fh:
            fh.write(b"{}\n")
        make_capsule_dir(capsule, ".seal")
        assert read_capsule_file(capsule / "outputs", "stdout.txt") == b"hello\n"
    assert (capsule / "trace.jsonl").read_bytes() == b"{}\n"


def test_the_pin_ends_with_its_scope_and_a_named_root_link_still_works(
    capsule: Path, tmp_path: Path
) -> None:
    latest = tmp_path / "runs" / "latest"
    latest.symlink_to(capsule.name, target_is_directory=True)
    with capsule_root_pin_scope():
        pin_capsule_root(capsule, _identity(capsule))
    # Outside the scope nothing is pinned, and a root the caller names through a
    # link (runs/latest) is opened by name, as the read-only commands document.
    with open_capsule_file(latest, "trace.jsonl") as fh:
        assert fh.read() == b""
    replace_capsule_file(latest, "outputs/x.txt", b"x")
    assert (capsule / "outputs" / "x.txt").read_bytes() == b"x"


def test_pinning_needs_a_scope(capsule: Path) -> None:
    with pytest.raises(RuntimeError):
        pin_capsule_root(capsule, _identity(capsule))


def test_with_capsule_root_pins_gives_each_call_its_own_scope(
    capsule: Path, tmp_path: Path
) -> None:
    @with_capsule_root_pins
    def pin_then_write(data: bytes) -> None:
        pin_capsule_root(capsule, _identity(capsule))
        replace_capsule_file(capsule, "outputs/x.txt", data)

    pin_then_write(b"1")
    with pytest.raises(RuntimeError):  # the scope ended with the call
        pin_capsule_root(capsule, _identity(capsule))


# ── hard links never carry a write ───────────────────────────────────────────


@pytest.mark.parametrize("mode", ["a", "w"])
def test_a_hard_linked_file_gets_a_private_copy_before_a_write(
    capsule: Path, tmp_path: Path, mode: str
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(ORIGINAL)
    os.chmod(outside, 0o640)
    os.link(outside, capsule / "scores.jsonl")
    with open_capsule_file_for_write(capsule, "scores.jsonl", mode=mode) as fh:  # type: ignore[arg-type]
        fh.write(b"NEW\n")
    assert outside.read_bytes() == ORIGINAL, f"mode {mode!r} wrote through the hard link"
    inside = capsule / "scores.jsonl"
    assert inside.stat().st_nlink == 1
    assert inside.stat().st_mode & 0o777 == 0o640
    assert inside.read_bytes() == (ORIGINAL + b"NEW\n" if mode == "a" else b"NEW\n")


def test_a_plain_file_is_still_written_in_place(capsule: Path) -> None:
    target = capsule / "trace.jsonl"
    target.write_bytes(b"one\n")
    inode = target.stat().st_ino
    with open_capsule_file_for_write(capsule, "trace.jsonl", mode="a") as fh:
        fh.write(b"two\n")
    with open_capsule_file_for_write(capsule, "outputs/w.txt", mode="w") as fh:
        fh.write(b"first\n")
    with open_capsule_file_for_write(capsule, "outputs/w.txt", mode="w") as fh:
        fh.write(b"second\n")
    assert target.stat().st_ino == inode
    assert target.read_bytes() == b"one\ntwo\n"
    assert (capsule / "outputs" / "w.txt").read_bytes() == b"second\n"


def test_make_capsule_dir_refuses_a_link(capsule: Path, tmp_path: Path) -> None:
    (capsule / ".seal").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CapsuleLinkError):
        make_capsule_dir(capsule, ".seal")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux capture path")
def test_capture_never_writes_into_a_capsule_swapped_after_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the orchestrator: the swap happens right after the sweep,
    as a process the workload left running could do it."""
    import novafabric.capture.orchestrator as orchestrator
    from novafabric.capture.orchestrator import CaptureOrchestrator

    real_sweep = orchestrator.inspect_capsule_after_workload
    outside = tmp_path / "outside"
    (outside / "outputs").mkdir(parents=True)

    def sweep_then_swap(capsule_dir: Path, **kwargs: object):  # type: ignore[no-untyped-def]
        report = real_sweep(capsule_dir, **kwargs)  # type: ignore[arg-type]
        capsule_dir.rename(tmp_path / f"moved-away-{capsule_dir.name}")
        capsule_dir.symlink_to(outside, target_is_directory=True)
        return report

    monkeypatch.setattr(orchestrator, "inspect_capsule_after_workload", sweep_then_swap)
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    with pytest.raises(CapsuleRootChangedError):
        CaptureOrchestrator(base_dir=tmp_path / "runs").run(
            [sys.executable, "-c", "print('payload that must not land outside')"]
        )
    assert _files(outside) == [], "capture wrote into the directory the capsule was swapped for"

    # `nova capture` reports it as a capture-integrity failure, not a traceback.
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    (tmp_path / "runs2").mkdir()
    result = CliRunner().invoke(
        app,
        ["capture", "--no-daemon", "--output-dir", str(tmp_path / "runs2"), "--",
         sys.executable, "-c", "print('x')"],
    )
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "capture integrity" in result.output + (result.stderr or "")
    assert _files(outside) == []
