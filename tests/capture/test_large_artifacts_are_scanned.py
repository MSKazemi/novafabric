"""An artifact over the in-memory scan limit is scanned in a stream, never skipped.

Review capture-runners-02: a file over ``MAX_ARTIFACT_SCAN_BYTES`` (64 MiB) was not
read at all — recorded ``skipped`` with ``findings: 0`` — and then digested and
sealed, so an API key printed anywhere in a long agent's stdout was sealed in
the clear, while the developer guide said the built-in scanner still covered it.
The window and overlap are shrunk here so keys land across window boundaries.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from novafabric.capture import secrets as secrets_mod
from novafabric.capture.secrets import SecretScannerV0

KEY = "sk-ant-api03-" + "Z" * 40
RUN = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


@pytest.fixture(autouse=True)
def _tiny_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets_mod, "MAX_ARTIFACT_SCAN_BYTES", 64)
    monkeypatch.setattr(secrets_mod, "_STREAM_WINDOW_BYTES", 96)
    monkeypatch.setattr(secrets_mod, "_STREAM_OVERLAP_BYTES", 80)


def _scan(capsule: Path) -> dict:  # type: ignore[type-arg]
    proof: dict = SecretScannerV0(capsule_dir=capsule, run_id=RUN).scan_and_redact()  # type: ignore[type-arg]
    return proof


def _target(proof: dict, ref: str) -> dict:  # type: ignore[type-arg]
    return next(t for t in proof["targets"] if t["ref"] == ref)


def _in_memory(tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    """The oracle: what the ordinary in-memory scan finds and writes for *body*."""
    with monkeypatch.context() as m:
        m.setattr(secrets_mod, "MAX_ARTIFACT_SCAN_BYTES", 1 << 30)
        ref_dir = tmp_path / "oracle"
        (ref_dir / "outputs").mkdir(parents=True)
        (ref_dir / "outputs" / "stdout.txt").write_text(body)
        target = _target(_scan(ref_dir), "outputs/stdout.txt")
        return target["findings_count"], (ref_dir / "outputs" / "stdout.txt").read_text()


@pytest.mark.parametrize("position", [0, 30, 90, 95, 150, 400])
def test_a_key_anywhere_in_a_large_text_file_is_redacted(
    tmp_path: Path, position: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = tmp_path / "capsule"
    out = capsule / "outputs" / "stdout.txt"
    out.parent.mkdir(parents=True)
    body = "x" * position + KEY + "\n" + "log line\n" * 60
    out.write_text(body)
    expected_count, expected_text = _in_memory(tmp_path, body, monkeypatch)
    proof = _scan(capsule)
    target = _target(proof, "outputs/stdout.txt")
    assert target["skipped"] is False
    assert target["findings_count"] == expected_count >= 1
    assert out.read_text() == expected_text  # byte-for-byte what the in-memory scan writes
    assert target["bytes_scanned"] == len(body)
    text = out.read_text()
    assert KEY not in text
    assert text.startswith("x" * position)
    assert text.endswith("log line\n")
    assert target["hash_after_redaction"] == "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def test_a_clean_large_file_is_left_byte_for_byte(tmp_path: Path) -> None:
    out = tmp_path / "outputs" / "stdout.txt"
    out.parent.mkdir()
    body = "nothing secret here\n" * 50
    out.write_text(body)
    target = _target(_scan(tmp_path), "outputs/stdout.txt")
    assert (target["skipped"], target["findings_count"]) == (False, 0)
    assert out.read_text() == body
    assert target["hash_before_redaction"] == target["hash_after_redaction"]


def test_several_keys_across_windows_are_all_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = tmp_path / "capsule"
    out = capsule / "outputs" / "stdout.txt"
    out.parent.mkdir(parents=True)
    body = (("y" * 37) + KEY + "\n") * 9
    out.write_text(body)
    expected_count, expected_text = _in_memory(tmp_path, body, monkeypatch)
    target = _target(_scan(capsule), "outputs/stdout.txt")
    assert target["findings_count"] == expected_count >= 9
    assert out.read_text() == expected_text
    assert KEY not in out.read_text()


def test_a_large_binary_carrying_a_key_is_dropped(tmp_path: Path) -> None:
    out = tmp_path / "outputs" / "blob.bin"
    out.parent.mkdir()
    out.write_bytes(b"\x00\x01" * 100 + KEY.encode() + b"\x00" * 200)
    target = _target(_scan(tmp_path), "outputs/blob.bin")
    assert target["binary"] is True and target["findings_count"] >= 1
    assert not out.exists()


def test_the_stream_finds_and_writes_exactly_what_the_in_memory_scan_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equivalence over many cut positions and several rule shapes."""
    keys = [
        KEY,
        "AKIA" + "QWERTYUIOPASDFGH",
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
        "Authorization: Bearer " + "eyJ" + "a" * 30 + "." + "b" * 30 + "." + "c" * 30,
    ]
    for shift in range(0, 160, 7):
        body = "".join(
            f"{'w' * ((shift + i * 13) % 50)} line {i} {keys[i % len(keys)]} tail\n"
            for i in range(12)
        )
        capsule = tmp_path / f"c{shift}"
        (capsule / "outputs").mkdir(parents=True)
        (capsule / "outputs" / "stdout.txt").write_text(body)
        oracle = tmp_path / f"o{shift}"
        count, text = _in_memory(oracle, body, monkeypatch)
        target = _target(_scan(capsule), "outputs/stdout.txt")
        assert (target["findings_count"], (capsule / "outputs" / "stdout.txt").read_text()) == (
            count, text,
        ), shift
