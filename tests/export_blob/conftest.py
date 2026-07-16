"""Shared fixtures for the batch capsule blob export suite (ADR-0141)."""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.evidence.signing import LocalSigner, generate_keypair


@pytest.fixture()
def capsule_root(tmp_path: Path) -> Path:
    root = tmp_path / "capsules"
    root.mkdir()
    return root


@pytest.fixture()
def signer(tmp_path: Path) -> LocalSigner:
    priv, _pub = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv)


@pytest.fixture()
def public_pem(signer: LocalSigner) -> bytes:
    return signer.public_pem
