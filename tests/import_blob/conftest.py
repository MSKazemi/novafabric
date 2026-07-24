"""Shared fixtures for the batch import suite (ADR-0207)."""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.evidence.signing import LocalSigner, generate_keypair


@pytest.fixture()
def keys(tmp_path: Path) -> tuple[Path, Path]:
    return generate_keypair(tmp_path / "keys")


@pytest.fixture()
def signer(keys: tuple[Path, Path]) -> LocalSigner:
    return LocalSigner(keys[0])


@pytest.fixture()
def public_pem(keys: tuple[Path, Path]) -> bytes:
    return keys[1].read_bytes()


@pytest.fixture()
def source_root(tmp_path: Path) -> Path:
    """Where original capsules live before export (the 'other instance')."""
    root = tmp_path / "source-capsules"
    root.mkdir()
    return root


@pytest.fixture()
def store_root(tmp_path: Path) -> Path:
    """The importing instance's capsule store (starts empty)."""
    root = tmp_path / "store"
    root.mkdir()
    return root


@pytest.fixture()
def receipts_dir(tmp_path: Path) -> Path:
    return tmp_path / "receipts"


@pytest.fixture()
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture()
def registry_db(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"
