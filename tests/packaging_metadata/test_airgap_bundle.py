"""Air-gap bundle format (ADR-0249 slice 1).

The acceptance criteria this slice owns: round-trip verification with zero
network; **tamper → fails naming the member**; a stowaway member (present
but unsigned) is a finding; the wrong key fails the signature, not the
hashes.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from novafabric.evidence.signing import generate_keypair
from novafabric.export_blob.airgap import (
    AirgapBundleError,
    build_bundle,
    verify_bundle,
)


@pytest.fixture()
def keys(tmp_path: Path) -> tuple[Path, bytes]:
    priv, pub = generate_keypair(tmp_path / "keys")
    return priv, pub.read_bytes()


@pytest.fixture()
def bundle(tmp_path: Path, keys: tuple[Path, bytes]) -> tuple[Path, bytes]:
    priv, pub_pem = keys
    (tmp_path / "a.whl").write_bytes(b"wheel-bytes")
    (tmp_path / "sbom.json").write_bytes(b'{"components": []}')
    out = build_bundle(
        tmp_path / "bundle.tar",
        {"wheels/a.whl": tmp_path / "a.whl", "sbom.json": tmp_path / "sbom.json"},
        signing_key=priv,
        nova_version="0.0.0-test",
    )
    return out, pub_pem


def test_round_trip_verifies(bundle: tuple[Path, bytes]) -> None:
    path, pub = bundle
    result = verify_bundle(path, public_key_pem=pub)
    assert result.ok, result.errors
    assert result.members_verified == 2


def test_tampered_member_fails_by_name(bundle: tuple[Path, bytes], tmp_path: Path) -> None:
    path, pub = bundle
    tampered = tmp_path / "tampered.tar"
    with tarfile.open(path) as src, tarfile.open(tampered, "w") as dst:
        for info in src.getmembers():
            data = src.extractfile(info).read()  # type: ignore[union-attr]
            if info.name == "wheels/a.whl":
                data = b"EVIL" + data[4:]
            info.size = len(data)
            dst.addfile(info, io.BytesIO(data))
    result = verify_bundle(tampered, public_key_pem=pub)
    assert not result.ok
    assert any("wheels/a.whl" in e and "hash mismatch" in e for e in result.errors)


def test_stowaway_member_is_a_finding(bundle: tuple[Path, bytes], tmp_path: Path) -> None:
    path, pub = bundle
    with_extra = tmp_path / "extra.tar"
    with tarfile.open(path) as src, tarfile.open(with_extra, "w") as dst:
        for info in src.getmembers():
            dst.addfile(info, src.extractfile(info))
        stow = tarfile.TarInfo("stowaway.bin")
        stow.size = 4
        dst.addfile(stow, io.BytesIO(b"boo!"))
    result = verify_bundle(with_extra, public_key_pem=pub)
    assert not result.ok
    assert any("stowaway.bin" in e and "not in the signed manifest" in e for e in result.errors)


def test_wrong_key_fails_signature(bundle: tuple[Path, bytes], tmp_path: Path) -> None:
    path, _ = bundle
    _, other_pub = generate_keypair(tmp_path / "other-keys")
    result = verify_bundle(path, public_key_pem=other_pub.read_bytes())
    assert not result.ok
    assert any("signature invalid" in e for e in result.errors)


def test_empty_bundle_refused(tmp_path: Path, keys: tuple[Path, bytes]) -> None:
    priv, _ = keys
    with pytest.raises(AirgapBundleError, match="empty"):
        build_bundle(tmp_path / "x.tar", {}, signing_key=priv, nova_version="0")


def test_reserved_names_refused(tmp_path: Path, keys: tuple[Path, bytes]) -> None:
    priv, _ = keys
    (tmp_path / "f").write_bytes(b"x")
    with pytest.raises(AirgapBundleError, match="reserved"):
        build_bundle(
            tmp_path / "x.tar",
            {"airgap-manifest.json": tmp_path / "f"},
            signing_key=priv,
            nova_version="0",
        )


def test_deterministic_given_created_at(tmp_path: Path, keys: tuple[Path, bytes]) -> None:
    """Same inputs + pinned timestamp → byte-identical manifest inventory
    (signatures over identical bytes; reproducibility is the air-gap ethos)."""
    priv, pub_pem = keys
    (tmp_path / "m.bin").write_bytes(b"data")
    a = build_bundle(
        tmp_path / "a.tar", {"m.bin": tmp_path / "m.bin"},
        signing_key=priv, nova_version="1", created_at=1700000000.0,
    )
    b = build_bundle(
        tmp_path / "b.tar", {"m.bin": tmp_path / "m.bin"},
        signing_key=priv, nova_version="1", created_at=1700000000.0,
    )
    with tarfile.open(a) as ta, tarfile.open(b) as tb:
        ma = ta.extractfile("airgap-manifest.json").read()  # type: ignore[union-attr]
        mb = tb.extractfile("airgap-manifest.json").read()  # type: ignore[union-attr]
    assert ma == mb
    assert verify_bundle(a, public_key_pem=pub_pem).ok
