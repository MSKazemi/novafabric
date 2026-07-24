"""Tests for the central SHA-256 helpers (enterprise-hardening Phase 2)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from novafabric._hashutil import (
    sha256_file_hex,
    sha256_file_prefixed,
    sha256_hex,
    sha256_prefixed,
)


def test_sha256_hex_matches_stdlib() -> None:
    data = b"novafabric"
    assert sha256_hex(data) == hashlib.sha256(data).hexdigest()


def test_sha256_prefixed_prefixes() -> None:
    data = b"novafabric"
    assert sha256_prefixed(data) == "sha256:" + hashlib.sha256(data).hexdigest()


def test_file_variants_match_bytes(tmp_path: Path) -> None:
    p = tmp_path / "blob.bin"
    payload = b"x" * (65536 * 3 + 7)  # spans multiple stream chunks
    p.write_bytes(payload)
    assert sha256_file_hex(p) == sha256_hex(payload)
    assert sha256_file_prefixed(p) == sha256_prefixed(payload)


def test_file_hex_is_streaming_bounded(tmp_path: Path, monkeypatch) -> None:
    """The file helper must not read the whole file at once (no path.read_bytes)."""
    p = tmp_path / "blob.bin"
    p.write_bytes(b"data")

    def _boom(self: Path) -> bytes:  # pragma: no cover - fails the test if hit
        raise AssertionError("read_bytes() called — helper is not streaming")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    # Should compute without ever calling Path.read_bytes.
    assert sha256_file_hex(p) == sha256_hex(b"data")
