"""Air-gap bundle format: build + verify fully offline (ADR-0249 slice 1).

One tar artifact whose members are inventoried in a DSSE-signed
``airgap-manifest.json`` — the same single-DSSE-writer stack every other
NovaFabric attestation uses (``evidence/intoto.py``), not a second signing
path. Verification needs **zero network**: the public key, the manifest, and
the bytes travel together.

Slice 1 is the *format/verifier* half (the repo's standing slice rule).
``scripts/build_airgap_bundle.py --closure-wheel`` adds the Python wheelhouse
closure on top of it, and ``scripts/verify_airgap_bundle.py`` is a
standard-library re-implementation of :func:`verify_bundle` that runs before
anything is installed (a test pins the two to the same format). The image,
chart, docs and advisory snapshot members are still to come (ADR-0249).
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_NAME = "airgap-manifest.json"
ENVELOPE_NAME = "airgap-manifest.dsse.json"
PAYLOAD_TYPE = "application/vnd.novafabric.airgap-manifest+json"
SCHEMA_VERSION = "0.1.0"


class AirgapBundleError(Exception):
    """Building or verifying an air-gap bundle failed; the message says where."""


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    members_verified: int
    errors: list[str]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


#: Key files are small; a member larger than this is not scanned for key material.
_KEY_SCAN_LIMIT = 1 << 20


def _signing_key_material(signing_key: Path) -> tuple[bytes, bytes]:
    """(raw 32-byte ed25519 seed, raw public key) of the PEM key at *signing_key*."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(signing_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):  # LocalSigner already refused this
        raise AirgapBundleError(f"{signing_key} is not an ed25519 private key")
    return key.private_bytes_raw(), key.public_key().public_bytes_raw()


def _holds_key(src: Path, seed: bytes, public_raw: bytes) -> bool:
    """True when *src* carries the signing key in any encoding we can read.

    Catches the raw seed anywhere in the file (raw, PKCS#8 DER), and any PEM,
    DER or OpenSSH private key whose public half is the signing key's. A copy
    encrypted under a passphrase cannot be recognised and is not caught.
    """
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if src.stat().st_size > _KEY_SCAN_LIMIT:
        return False
    data = src.read_bytes()
    if seed in data:
        return True
    loaders = (
        serialization.load_pem_private_key,
        serialization.load_der_private_key,
        serialization.load_ssh_private_key,
    )
    for loader in loaders:
        try:
            key = loader(data, None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            continue
        if (
            isinstance(key, Ed25519PrivateKey)
            and key.public_key().public_bytes_raw() == public_raw
        ):
            return True
    return False


def build_bundle(
    out_path: Path,
    members: dict[str, Path],
    *,
    signing_key: Path,
    nova_version: str,
    created_at: float | None = None,
) -> Path:
    """Write ``out_path`` (tar) containing *members* + the signed manifest.

    ``members`` maps the in-bundle path → source file. The manifest inventories
    every member with its SHA-256 and size; the DSSE envelope over the
    manifest is signed with the ed25519 key at *signing_key* (the
    ``evidence/signing.LocalSigner`` format). Unsigned bundles are not a
    thing — a bundle that cannot be verified offline defeats its purpose.
    """
    from novafabric.evidence.intoto import dsse_sign_payload
    from novafabric.evidence.signing import LocalSigner

    if not members:
        raise AirgapBundleError("refusing to build an empty bundle")
    signer = LocalSigner(signing_key)
    seed, public_raw = _signing_key_material(signing_key)
    for arcname, src in members.items():
        if not src.is_file():
            raise AirgapBundleError(f"member {arcname!r}: source {src} is not a file")
        if arcname in (MANIFEST_NAME, ENVELOPE_NAME):
            raise AirgapBundleError(f"member name {arcname!r} is reserved")
        # The signing secret must never travel with the bundle it signs: not the
        # key file itself, and not the same key under another name or encoding.
        if src.resolve() == signing_key.resolve() or _holds_key(src, seed, public_raw):
            raise AirgapBundleError(
                f"member {arcname!r} is the signing key; it must never enter the bundle"
            )

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "novafabric-airgap-bundle",
        "nova_version": nova_version,
        "created_at": created_at if created_at is not None else time.time(),
        "members": [
            {
                "path": arcname,
                "sha256": _sha256_file(src),
                "size": src.stat().st_size,
            }
            for arcname, src in sorted(members.items())
        ],
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
    envelope = dsse_sign_payload(manifest_bytes, PAYLOAD_TYPE, signer)
    envelope_bytes = json.dumps(envelope, indent=2, sort_keys=True).encode()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w") as tar:
        for arcname, src in sorted(members.items()):
            tar.add(src, arcname=arcname, recursive=False)
        for name, blob in ((MANIFEST_NAME, manifest_bytes), (ENVELOPE_NAME, envelope_bytes)):
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.mtime = int(manifest["created_at"])
            import io

            tar.addfile(info, io.BytesIO(blob))
    return out_path


def verify_bundle(bundle_path: Path, *, public_key_pem: bytes) -> VerifyResult:
    """Verify *bundle_path* fully offline; every failure is named, none is fatal
    to the report (an auditor wants the complete list, not the first hit)."""
    import base64

    from novafabric.evidence.intoto import dsse_verify
    from novafabric.evidence.signing import verify_with_pem

    errors: list[str] = []
    verified = 0
    with tarfile.open(bundle_path, "r") as tar:
        names = set(tar.getnames())
        for required in (MANIFEST_NAME, ENVELOPE_NAME):
            if required not in names:
                return VerifyResult(False, 0, [f"bundle is missing {required}"])

        manifest_bytes = tar.extractfile(MANIFEST_NAME).read()  # type: ignore[union-attr]
        envelope = json.loads(
            tar.extractfile(ENVELOPE_NAME).read()  # type: ignore[union-attr]
        )

        # 1. Signature over the manifest, offline (ed25519 PEM helper from
        # the evidence stack; byte-exact payload comparison, not dict-equal).
        try:
            dsse_verify(
                envelope,
                lambda pae, sig: verify_with_pem(public_key_pem, pae, sig),
            )
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            return VerifyResult(False, 0, [f"manifest signature invalid: {exc}"])
        if base64.b64decode(envelope["payload"]) != manifest_bytes:
            return VerifyResult(
                False, 0, ["manifest bytes do not match the signed payload"]
            )

        manifest = json.loads(manifest_bytes)

        # 2. Every inventoried member present and hash-exact; every extra
        # member named (an unsigned stowaway is a finding, not a shrug).
        inventoried = {m["path"] for m in manifest["members"]}
        extras = names - inventoried - {MANIFEST_NAME, ENVELOPE_NAME}
        for extra in sorted(extras):
            errors.append(f"member {extra!r} is present but not in the signed manifest")
        for member in manifest["members"]:
            path = member["path"]
            fobj = tar.extractfile(path) if path in names else None
            if fobj is None:
                errors.append(f"member {path!r} is in the manifest but missing")
                continue
            digest = hashlib.sha256(fobj.read()).hexdigest()
            if digest != member["sha256"]:
                errors.append(
                    f"member {path!r} hash mismatch: manifest {member['sha256'][:12]}…, "
                    f"actual {digest[:12]}…"
                )
                continue
            verified += 1

    return VerifyResult(not errors, verified, errors)
