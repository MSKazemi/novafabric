"""The evidence detail route shows only bytes it verified (review r8, hole 5).

(a) ``GET /api/evidence/{id}`` verified the bundle, then re-read ``manifest.json`` and
    the run envelope through the same descriptor: a bundle rewritten in place between
    the two reads showed forged content next to ``verified: true``. The route now
    verifies and displays one private snapshot of the archive.
(b) ``signing_key_fingerprint`` hashed ``signatures/run.intoto.json.cert``, a path no
    real bundle has, so it was always null. It is now the fingerprint of the key whose
    signature verified (the verifier's ``signer_fingerprints``).
(c) ``BundleVerdictCache`` keyed a verdict on (inode, size, mtime, ctime). Where
    ctime is coarse (Lustre: 1 s), an in-place same-size rewrite with the mtime
    restored reused a stale ``verified: true``. The key is now the archive's SHA-256.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from novafabric.evidence.bundle import EvidenceBundleBuilder
from novafabric.evidence.bundle_verify import _public_key_fingerprint
from novafabric.evidence.signing import LocalSigner, generate_keypair
from novafabric.serve import evidence_verify

from .test_evidence_list_detail_verdicts import (
    AUTH,
    HOST,
    RUN_ID,
    VICTIM,
    _capsule,
    _relabel,
    create_app_for,
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(home / "audit.jsonl"))
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence))
    for name in ("NOVAFABRIC_SEAL_CONFIG", "NOVAFABRIC_SEAL_DB_PATH", "NOVAFABRIC_CAPSULE_DIR"):
        monkeypatch.delenv(name, raising=False)
    priv, pub = generate_keypair(tmp_path / "keys")
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_TRUSTED_KEYS", str(pub))
    genuine = EvidenceBundleBuilder(
        _capsule(tmp_path / "src"), LocalSigner(priv), evidence / "genuine.zip",
        audit_log_path=tmp_path / "audit.jsonl",
    ).build()
    forged = tmp_path / "forged.zip"
    _relabel(genuine, forged)
    runs = tmp_path / "runs"
    runs.mkdir()
    client = TestClient(create_app_for(runs, tmp_path), raise_server_exceptions=False)
    return {"client": client, "genuine": genuine, "forged": forged, "pub": pub}


def _rewrite_in_place(path: Path, data: bytes) -> None:
    with open(path, "r+b") as handle:
        handle.write(data)
        handle.truncate()


def test_a_rewrite_after_verification_never_shows_beside_verified_true(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = evidence_verify.verify_bundle_handle
    forged = env["forged"].read_bytes()

    def verify_then_rewrite(*args: Any, **kwargs: Any) -> Any:
        check = real(*args, **kwargs)
        _rewrite_in_place(env["genuine"], forged)  # the attacker wins the race
        return check

    monkeypatch.setattr(evidence_verify, "verify_bundle_handle", verify_then_rewrite)
    body = env["client"].get("/api/evidence/genuine", params=AUTH, headers=HOST).json()

    assert body["verified"] is True
    assert body["manifest"].get("run_id") != VICTIM, "forged manifest shown as verified"
    assert body["run_id"] == RUN_ID


def test_the_fingerprint_is_the_key_that_verified(env: dict[str, Any]) -> None:
    body = env["client"].get("/api/evidence/genuine", params=AUTH, headers=HOST).json()
    expected = _public_key_fingerprint(env["pub"].read_bytes())
    assert body["signing_key_fingerprint"] == expected
    assert body["signer_fingerprints"] == [expected]
    assert body["members_signed"] is True
    assert body["unsigned_members"] == []


def test_a_same_identity_rewrite_is_reverified_even_with_coarse_timestamps(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freeze fstat — inode, size, mtime and ctime all unchanged, as on a 1 s-ctime FS."""
    frozen = os.stat(env["genuine"])
    monkeypatch.setattr(evidence_verify.os, "fstat", lambda _fd: frozen)
    cache = evidence_verify.BundleVerdictCache()
    anchors = [env["pub"].read_bytes()]

    target = env["genuine"].with_name("same-identity.zip")

    def verdict(data: bytes) -> evidence_verify.BundleVerdict:
        target.write_bytes(data)
        with open(target, "rb") as handle:
            return cache.verdict(
                handle, "b", trusted_public_pems=anchors,
                tsa_chain_check=lambda _t, _b: None,
            )

    genuine = env["genuine"].read_bytes()
    assert verdict(genuine).verified is True
    flipped = bytearray(genuine)
    flipped[len(flipped) // 2] ^= 0xFF
    assert verdict(bytes(flipped)).verified is False
    assert verdict(env["forged"].read_bytes()).verified is False
    assert verdict(genuine).verified is True  # a cache hit for the genuine bytes


def test_the_list_reports_member_coverage(env: dict[str, Any]) -> None:
    listed = env["client"].get("/api/evidence", params=AUTH, headers=HOST).json()
    (entry,) = [b for b in listed["bundles"] if b["bundle_id"] == "genuine"]
    assert entry["verified"] is True
    assert entry["members_signed"] is True
    with zipfile.ZipFile(env["genuine"]) as zf:
        created = json.loads(zf.read("manifest.json"))["created_at"]
    assert entry["timestamp"] == created
