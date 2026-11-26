"""The raw-evidence copier and verifier refuse unsafe input (issue #59, PR review).

``collect_evidence`` copies files out of a harness-owned scratch tree and
``verify_evidence`` re-hashes them later from a manifest that may have been edited. Both
treat the filesystem and the manifest as untrusted: no symlink is followed, no path
leaves the evidence root, no special file is read, and nothing is unbounded. A filename
allowlist alone is not the boundary -- an allowlisted *name* can be a symlink.

POSIX only (``O_NOFOLLOW`` and directory file descriptors).
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from replay.fidelity import _report

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW (POSIX)")

SECRET = b"TOP-SECRET-NOT-EVIDENCE\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    """A scratch tree shaped like the harness's, plus a secret outside it."""
    root = tmp_path / "src"
    scenario = root / "S1"
    (scenario / "runs" / "01RUN").mkdir(parents=True)
    (scenario / "replays" / "01REP").mkdir(parents=True)
    (scenario / "home").mkdir()
    (scenario / "markers.capture").write_text("start capture\nend capture rc=0\n")
    (scenario / "runs" / "01RUN" / "model-calls.jsonl").write_text("{}\n")
    (scenario / "replays" / "01REP" / "replay_result.yaml").write_text("status: success\n")
    (scenario / "home" / "registry.db").write_text("never copied")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_bytes(SECRET)
    return {"root": root, "scenario": scenario, "dest": tmp_path / "evidence", "secret": secret}


def _collect(t: dict[str, Path], **limits: int) -> dict[str, str]:
    return _report.collect_evidence(t["root"], t["dest"], **limits)


def _nothing_copied(t: dict[str, Path]) -> None:
    dest = t["dest"]
    leaked = [p for p in dest.rglob("*") if p.is_file()] if dest.exists() else []
    assert leaked == [], leaked
    assert not any(SECRET in p.read_bytes() for p in dest.rglob("*") if p.is_file())


# ── collection ───────────────────────────────────────────────────────────────


def test_a_normal_tree_is_collected_and_hashed(tree: dict[str, Path]) -> None:
    manifest = _collect(tree)
    assert sorted(manifest) == [
        "S1/markers.capture",
        "S1/replays/01REP/replay_result.yaml",
        "S1/runs/01RUN/model-calls.jsonl",
    ]
    for rel, digest in manifest.items():
        assert _sha((tree["dest"] / rel).read_bytes()) == digest
    assert _report.verify_evidence(tree["dest"], manifest) == []


def test_an_allowlisted_name_that_is_a_symlink_to_a_secret_is_refused(
    tree: dict[str, Path],
) -> None:
    link = tree["scenario"] / "sentinel.capture"
    link.symlink_to(tree["secret"])
    with pytest.raises(_report.EvidenceError, match="symlink"):
        _collect(tree)
    _nothing_copied(tree)


def test_a_symlinked_evidence_file_inside_a_run_directory_is_refused(
    tree: dict[str, Path],
) -> None:
    (tree["scenario"] / "runs" / "01RUN" / "tool-calls.jsonl").symlink_to(tree["secret"])
    with pytest.raises(_report.EvidenceError, match="symlink"):
        _collect(tree)
    _nothing_copied(tree)


def test_a_symlinked_runs_directory_is_not_followed(tree: dict[str, Path]) -> None:
    other = tree["secret"].parent / "elsewhere" / "01X"
    other.mkdir(parents=True)
    (other / "model-calls.jsonl").write_bytes(SECRET)
    (tree["scenario"] / "runs-mutated").symlink_to(other.parent)
    with pytest.raises(_report.EvidenceError, match="symlink"):
        _collect(tree)
    _nothing_copied(tree)


def test_a_symlinked_run_id_directory_is_not_followed(tree: dict[str, Path]) -> None:
    other = tree["secret"].parent / "run"
    other.mkdir()
    (other / "model-calls.jsonl").write_bytes(SECRET)
    (tree["scenario"] / "replays" / "01EVIL").symlink_to(other)
    with pytest.raises(_report.EvidenceError, match="symlink"):
        _collect(tree)
    _nothing_copied(tree)


def test_a_symlinked_scenario_directory_is_not_followed(tree: dict[str, Path]) -> None:
    other = tree["secret"].parent / "scen"
    other.mkdir()
    (other / "markers.capture").write_bytes(SECRET)
    (tree["root"] / "S2").symlink_to(other)
    with pytest.raises(_report.EvidenceError, match="symlink"):
        _collect(tree)
    _nothing_copied(tree)


def test_a_fifo_with_an_evidence_name_is_refused_without_blocking(
    tree: dict[str, Path],
) -> None:
    os.mkfifo(tree["scenario"] / "model-hits.capture")
    with pytest.raises(_report.EvidenceError, match="regular file"):
        _collect(tree)
    _nothing_copied(tree)


def test_a_socket_with_an_evidence_name_is_refused(tree: dict[str, Path]) -> None:
    import socket

    path = tree["scenario"] / "sentinel.replay"
    srv = socket.socket(socket.AF_UNIX)
    try:
        srv.bind(str(path))
        assert stat.S_ISSOCK(path.lstat().st_mode)
        with pytest.raises(_report.EvidenceError, match="regular file"):
            _collect(tree)
    finally:
        srv.close()


def test_a_directory_with_an_evidence_file_name_is_refused(tree: dict[str, Path]) -> None:
    (tree["scenario"] / "markers.replay").mkdir()
    with pytest.raises(_report.EvidenceError, match="regular file"):
        _collect(tree)


def test_a_hard_link_is_just_a_regular_file_and_stays_inside_the_tree(
    tree: dict[str, Path],
) -> None:
    """Hard links cannot escape by name; the copy is of the bytes at the confined path."""
    os.link(tree["scenario"] / "markers.capture", tree["scenario"] / "markers.replay")
    assert "S1/markers.replay" in _collect(tree)


def test_an_oversized_file_is_refused(tree: dict[str, Path]) -> None:
    (tree["scenario"] / "markers.big").write_bytes(b"x" * 2048)
    with pytest.raises(_report.EvidenceError, match="byte"):
        _collect(tree, max_file_bytes=1024)
    _nothing_copied(tree)


def test_the_aggregate_size_is_bounded(tree: dict[str, Path]) -> None:
    for i in range(4):
        (tree["scenario"] / f"markers.p{i}").write_bytes(b"y" * 600)
    with pytest.raises(_report.EvidenceError, match="total"):
        _collect(tree, max_total_bytes=2000)
    _nothing_copied(tree)


def test_the_file_count_is_bounded(tree: dict[str, Path]) -> None:
    for i in range(10):
        (tree["scenario"] / f"markers.n{i}").write_text("x")
    with pytest.raises(_report.EvidenceError, match="more than"):
        _collect(tree, max_files=5)
    _nothing_copied(tree)


def test_collection_never_overwrites_existing_evidence(tree: dict[str, Path]) -> None:
    _collect(tree)
    target = tree["dest"] / "S1" / "markers.capture"
    original = target.read_bytes()
    target.write_bytes(b"historical evidence")
    with pytest.raises(_report.EvidenceError, match="exists"):
        _collect(tree)
    assert target.read_bytes() == b"historical evidence" != original


def test_a_destination_symlink_is_not_written_through(tree: dict[str, Path]) -> None:
    victim = tree["secret"].parent / "victim.txt"
    victim.write_text("keep")
    (tree["dest"] / "S1").mkdir(parents=True)
    (tree["dest"] / "S1" / "markers.capture").symlink_to(victim)
    with pytest.raises(_report.EvidenceError):
        _collect(tree)
    assert victim.read_text() == "keep"


def test_every_evidence_open_refuses_to_follow_symlinks(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation guard: drop O_NOFOLLOW from any open and this fails."""
    real_open = os.open
    seen: list[int] = []

    def spy(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        seen.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    manifest = _collect(tree)
    _report.verify_evidence(tree["dest"], manifest)
    assert seen, "no open() was observed"
    assert all(f & os.O_NOFOLLOW for f in seen), [hex(f) for f in seen if not f & os.O_NOFOLLOW]


def test_a_failed_collection_leaves_no_partial_copy(tree: dict[str, Path]) -> None:
    (tree["scenario"] / "sentinel.capture").symlink_to(tree["secret"])  # sorts after markers
    with pytest.raises(_report.EvidenceError):
        _collect(tree)
    _nothing_copied(tree)


# ── verification ─────────────────────────────────────────────────────────────


@pytest.fixture
def kept(tree: dict[str, Path]) -> tuple[Path, dict[str, str]]:
    manifest = _collect(tree)
    return tree["dest"], manifest


def test_a_changed_file_fails_verification(kept: tuple[Path, dict[str, str]]) -> None:
    dest, manifest = kept
    (dest / "S1" / "markers.capture").write_text("tampered\n")
    assert _report.verify_evidence(dest, manifest) == ["S1/markers.capture"]


def test_a_missing_expected_file_fails_verification(kept: tuple[Path, dict[str, str]]) -> None:
    dest, manifest = kept
    (dest / "S1" / "markers.capture").unlink()
    assert _report.verify_evidence(dest, manifest) == ["S1/markers.capture"]


def test_a_file_replaced_by_a_symlink_with_the_same_bytes_still_fails(
    kept: tuple[Path, dict[str, str]],
) -> None:
    """Substitution after hashing: identical content behind a symlink is not the evidence."""
    dest, manifest = kept
    target = dest / "S1" / "markers.capture"
    data = target.read_bytes()
    elsewhere = dest.parent / "same-bytes.txt"
    elsewhere.write_bytes(data)
    target.unlink()
    target.symlink_to(elsewhere)
    assert _report.verify_evidence(dest, manifest, strict=False) == ["S1/markers.capture"]
    # strict mode also names the unsafe object itself
    assert "unsafe:S1/markers.capture" in _report.verify_evidence(dest, manifest)


def test_a_symlinked_parent_directory_fails_verification(
    kept: tuple[Path, dict[str, str]],
) -> None:
    dest, manifest = kept
    real = dest / "S1" / "runs"
    moved = dest.parent / "moved-runs"
    real.rename(moved)
    real.symlink_to(moved)
    assert "S1/runs/01RUN/model-calls.jsonl" in _report.verify_evidence(dest, manifest)


@pytest.mark.parametrize(
    "rel",
    [
        "../secret.txt",
        "S1/../../outside/secret.txt",
        "/etc/passwd",
        "S1/./markers.capture",
        "S1//markers.capture",
        "S1\\markers.capture",
        "S1/markers.capture\x00",
        "",
        ".",
        "..",
        "S1/runs/../markers.capture",
        "S1/unexpected.txt",
        "S1/markers.",
        "./S1/markers.capture",
    ],
)
def test_noncanonical_or_unexpected_manifest_paths_are_rejected_unread(
    tree: dict[str, Path], rel: str
) -> None:
    kept_dest = tree["dest"]
    _collect(tree)
    # the secret's real hash is offered: an implementation that resolves the path would accept it
    manifest = {rel: _sha(SECRET)}
    assert _report.verify_evidence(kept_dest, manifest, strict=False) == [rel]


def test_manifest_traversal_cannot_read_a_file_outside_the_evidence_root(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _collect(tree)
    opened: list[str] = []
    real_open = os.open

    def spy(path: Any, flags: int, *a: Any, **k: Any) -> int:
        opened.append(str(path))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    _report.verify_evidence(tree["dest"], {"../outside/secret.txt": _sha(SECRET)})
    assert not any("secret" in p for p in opened), opened


@pytest.mark.parametrize("digest", ["", "abc", "G" * 64, "A" * 64, None, 5, ["x"]])
def test_a_malformed_digest_is_a_failure_not_a_crash(
    kept: tuple[Path, dict[str, str]], digest: Any
) -> None:
    dest, manifest = kept
    bad = {**manifest, "S1/markers.capture": digest}
    assert _report.verify_evidence(dest, bad) == ["S1/markers.capture"]


def test_non_string_manifest_keys_and_a_non_dict_manifest_fail_closed(
    kept: tuple[Path, dict[str, str]],
) -> None:
    dest, _ = kept
    assert _report.verify_evidence(dest, {5: "0" * 64}, strict=False) == ["5"]  # type: ignore[dict-item]
    assert _report.verify_evidence(dest, ["S1/markers.capture"]) == ["<manifest>"]  # type: ignore[arg-type]
    assert _report.verify_evidence(dest, None) == ["<manifest>"]  # type: ignore[arg-type]


def test_an_evidence_file_over_the_size_limit_fails_verification(
    kept: tuple[Path, dict[str, str]],
) -> None:
    dest, manifest = kept
    big = dest / "S1" / "markers.capture"
    big.write_bytes(b"z" * 4096)
    manifest = {**manifest, "S1/markers.capture": _sha(b"z" * 4096)}
    assert _report.verify_evidence(dest, manifest, max_file_bytes=1024) == ["S1/markers.capture"]
    assert _report.verify_evidence(dest, manifest) == []


def test_an_unlisted_evidence_file_is_reported_in_strict_mode(
    kept: tuple[Path, dict[str, str]],
) -> None:
    dest, manifest = kept
    (dest / "S1" / "markers.injected").write_text("not in the manifest")
    assert _report.verify_evidence(dest, manifest) == ["unlisted:S1/markers.injected"]
    assert _report.verify_evidence(dest, manifest, strict=False) == []


def test_a_missing_evidence_directory_fails_every_entry(tmp_path: Path) -> None:
    manifest = {"S1/markers.capture": "0" * 64}
    assert _report.verify_evidence(tmp_path / "nope", manifest) == ["S1/markers.capture"]


def test_verification_does_not_depend_on_the_working_directory(
    kept: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dest, manifest = kept
    monkeypatch.chdir(tmp_path)
    assert _report.verify_evidence(dest, manifest) == []


# ── the command line ─────────────────────────────────────────────────────────


def test_cli_verify_rejects_a_baseline_whose_manifest_points_outside(
    tmp_path: Path,
) -> None:
    import json

    baseline = tmp_path / "run-01.json"
    (tmp_path / "run-01.evidence").mkdir()
    (tmp_path / "secret.txt").write_bytes(SECRET)
    baseline.write_text(
        json.dumps({"scenarios": {}, "evidence": {"files": {"../secret.txt": _sha(SECRET)}}})
    )
    assert _report.main([str(baseline), "--verify"]) == 1


def test_cli_refuses_an_oversized_baseline_file(tmp_path: Path) -> None:
    baseline = tmp_path / "run-01.json"
    baseline.write_bytes(b" " * (_report.MAX_BASELINE_BYTES + 1))
    assert _report.main([str(baseline)]) == 2


# ── the layers behind the walk: each must hold on its own ────────────────────


def test_open_regular_refuses_a_directory_and_a_fifo_itself(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    os.mkfifo(tmp_path / "f")
    dfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in ("d", "f"):
            with pytest.raises(_report.EvidenceError, match="regular file"):
                _report._open_regular(name, dfd, 1024)
    finally:
        os.close(dfd)


def test_open_regular_refuses_an_oversized_file_before_reading_it(tmp_path: Path) -> None:
    (tmp_path / "big").write_bytes(b"x" * 100)
    dfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(_report.EvidenceError, match="limit"):
            _report._open_regular("big", dfd, 10)
    finally:
        os.close(dfd)


def test_the_stream_stops_a_file_that_grows_past_the_limit_while_it_is_read(
    tmp_path: Path,
) -> None:
    """The size seen at open can be stale: the read itself enforces the bound."""
    path = tmp_path / "grows"
    path.write_bytes(b"x" * 300_000)
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(_report.EvidenceError, match="more than"):
            _report._stream(fd, 100_000)
    finally:
        os.close(fd)


def test_traversal_is_rejected_even_if_the_layout_pattern_were_loosened(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import re

    _collect(tree)
    monkeypatch.setattr(_report, "_EVIDENCE", re.compile(r".+"))
    manifest = {"../outside/secret.txt": _sha(SECRET), "S1/./markers.capture": _sha(b"x")}
    assert _report.verify_evidence(tree["dest"], manifest, strict=False) == sorted(manifest)


def test_a_malformed_digest_is_rejected_without_reading_the_file(
    kept: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    dest, manifest = kept
    reads: list[str] = []
    real = _report._hash_confined

    def spy(root_fd: int, rel: str, max_bytes: int) -> str | None:
        reads.append(rel)
        return real(root_fd, rel, max_bytes)

    monkeypatch.setattr(_report, "_hash_confined", spy)
    bad = {**manifest, "S1/markers.capture": "not-a-digest"}
    assert "S1/markers.capture" in _report.verify_evidence(dest, bad, strict=False)
    assert "S1/markers.capture" not in reads
