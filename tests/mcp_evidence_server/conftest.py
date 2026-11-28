"""Capsule builders for the MCP evidence server tests (issue #244)."""

from __future__ import annotations

import datetime
import hashlib
import json
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.capture.finalize import evidence_digests
from novafabric.trust.novaseal import KeyConfig, NovaSeal

#: A string the default ADR-0009 rule pack masks (``openai-api-key``).
SECRET = "sk-proj-" + "A1b2C3d4" * 6
#: A prompt that must never appear in any tool output.
PROMPT = "PLEASE-NEVER-LEAK-THIS-PROMPT"


def write_capsule(root: Path, name: str, **manifest: Any) -> Path:
    """A minimal unsealed capsule ``root/name`` with a model-call record."""
    capsule = root / name
    capsule.mkdir(parents=True)
    (capsule / "model-calls.jsonl").write_text(
        json.dumps({"model": "m", "messages": [{"role": "user", "content": PROMPT}]}) + "\n"
    )
    data: dict[str, Any] = {
        "run_id": name,
        "status": "success",
        "created_at": "2026-11-27T01:02:03Z",
        "model_call_count": 1,
        "command": ["python", "agent.py", "--api-key", SECRET],
        "working_directory": str(root),
        "host": "build-host-7",
    }
    data.update(manifest)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(data))
    return capsule


def seal(capsule: Path, work: Path) -> None:
    """Seal *capsule* with a throwaway self-signed key, the way capture does."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp-evidence-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    work.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = work / f"{capsule.name}.key", work / f"{capsule.name}.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    manifest["evidence_digests"] = evidence_digests(capsule)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    sealer = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(work / f"{capsule.name}-merkle.db"),
    )
    try:
        bundle = sealer.seal(manifest)
    finally:
        # Close the sealer's SQLite Merkle log now. Left to garbage collection,
        # its close (a WAL checkpoint that grows the .db and removes -wal/-shm)
        # lands at a random later moment, such as in the middle of a test that
        # snapshots tmp_path to prove the server wrote nothing.
        sealer._merkle.close()
    seal_dir = capsule / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry))


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    """An empty scope directory."""
    directory = tmp_path / "runs"
    directory.mkdir()
    return directory


@pytest.fixture
def sealed(tmp_path: Path) -> Callable[[Path, str], Path]:
    """Build and seal a capsule: ``sealed(root, name)``."""

    def _make(root: Path, name: str) -> Path:
        capsule = write_capsule(root, name)
        seal(capsule, tmp_path / "seal-work")
        return capsule

    return _make


def snapshot(tree: Path) -> dict[str, tuple[str, int, int, str]]:
    """``{relative path: (kind, size, mtime_ns, sha256)}`` for every entry under *tree*.

    Links are not followed. Regular files are hashed, so a rewrite that keeps
    the size and mtime is still seen.
    """
    out: dict[str, tuple[str, int, int, str]] = {}
    for path in sorted(tree.rglob("*")):
        st = path.lstat()
        if stat.S_ISREG(st.st_mode):
            kind, digest = "file", hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISDIR(st.st_mode):
            kind, digest = "dir", ""
        else:
            kind, digest = "other", ""
        out[str(path.relative_to(tree))] = (kind, st.st_size, st.st_mtime_ns, digest)
    return out
