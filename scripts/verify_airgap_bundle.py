#!/usr/bin/env python3
"""Verify a NovaFabric air-gap bundle fully offline, before anything is installed.

Standard library only: this file is shipped inside every closure bundle
(``scripts/build_airgap_bundle.py --closure-wheel``) and must run on a host
where neither NovaFabric nor any of its dependencies is installed yet.

What it checks (ADR-0249; the format is ``novafabric.export_blob.airgap``):

1. the DSSE signature over ``airgap-manifest.json`` with the ed25519 public
   key you trust (distributed to you out of band, never taken from the
   bundle), using the ``cryptography`` package if it happens to be importable,
   else the ``openssl`` command line (OpenSSL 3.0 or later);
2. that the signed payload is byte-identical to the manifest in the tar;
3. every member's SHA-256 and size; a member listed but absent, a member
   present but not listed (an unsigned stowaway), a duplicated name, a
   non-regular tar entry (link, device, directory) or an unsafe path
   (absolute, ``..``) each fail, by name;
4. when the bundle carries ``SHA256SUMS``, that it agrees line for line with
   the signed manifest.

With ``--extract-to DIR`` the members are written into a fresh staging
directory while they are hashed, and the directory is renamed to ``DIR``
only when every check passed: what you then install is exactly the bytes
that were verified. On any failure nothing is left behind.

``--integrity-only`` skips step 1. It detects corruption in transit, not a
substituted bundle: anyone who can rewrite the bundle can rewrite its hashes.

Usage:
    python3 verify_airgap_bundle.py --bundle novafabric-airgap.tar \\
        --public-key airgap-signing.pub.pem --extract-to ./novafabric-airgap

Exit codes: 0 verified, 1 verification failed (or the signature could not be
checked), 2 usage error.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

# Kept identical to novafabric/export_blob/airgap.py (a test pins the parity).
MANIFEST_NAME = "airgap-manifest.json"
ENVELOPE_NAME = "airgap-manifest.dsse.json"
PAYLOAD_TYPE = "application/vnd.novafabric.airgap-manifest+json"
MANIFEST_KIND = "novafabric-airgap-bundle"
SUMS_NAME = "SHA256SUMS"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1 << 20


class SignatureUnavailable(Exception):
    """No usable ed25519 verifier on this host; the bundle is NOT verified."""


@dataclass
class Report:
    members_verified: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    signature_checked: bool = False
    extracted_to: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def dsse_pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE Pre-Authentication Encoding, byte-identical to evidence/intoto.py."""
    type_bytes = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(type_bytes), type_bytes, len(payload), payload)


# ── signature backends ───────────────────────────────────────────────────────


def _verify_with_cryptography(public_pem: bytes, message: bytes, signature: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = serialization.load_pem_public_key(public_pem)
    except UnsupportedAlgorithm as exc:
        raise ValueError(f"unsupported public key: {exc}") from None
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("public key is not ed25519")
    try:
        key.verify(signature, message)
    except InvalidSignature:
        return False
    return True


def _verify_with_openssl(public_pem: bytes, message: bytes, signature: bytes) -> bool:
    openssl = shutil.which("openssl")
    if openssl is None:
        raise SignatureUnavailable("the openssl command is not on PATH")
    with tempfile.TemporaryDirectory(prefix="nova-airgap-verify-") as tmp:
        key = Path(tmp, "key.pem")
        msg = Path(tmp, "message.bin")
        sig = Path(tmp, "signature.bin")
        key.write_bytes(public_pem)
        msg.write_bytes(message)
        sig.write_bytes(signature)
        kind = subprocess.run(
            [openssl, "pkey", "-pubin", "-in", str(key), "-noout", "-text"],
            capture_output=True,
            text=True,
            check=False,
        )
        if kind.returncode != 0:
            raise ValueError(f"openssl cannot read the public key: {kind.stderr.strip()}")
        if not kind.stdout.lstrip().upper().startswith("ED25519"):
            raise ValueError("public key is not ed25519")
        run = subprocess.run(
            [
                openssl,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(key),
                "-rawin",
                "-in",
                str(msg),
                "-sigfile",
                str(sig),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    out = run.stdout + run.stderr
    if run.returncode == 0 and "Verified Successfully" in out:
        return True
    if "Verification Failure" in out:
        return False
    # e.g. OpenSSL 1.1 has no -rawin: that is "cannot check", not "forged".
    raise SignatureUnavailable(f"openssl could not check the signature: {out.strip()}")


def verify_ed25519(
    public_pem: bytes, message: bytes, signature: bytes, *, backend: str = "auto"
) -> bool:
    """True/False for a valid/invalid signature; raises when it cannot decide."""
    if backend in ("auto", "cryptography"):
        try:
            return _verify_with_cryptography(public_pem, message, signature)
        except ImportError:
            if backend == "cryptography":
                raise SignatureUnavailable("the cryptography package is not importable") from None
    if backend in ("auto", "openssl"):
        return _verify_with_openssl(public_pem, message, signature)
    raise ValueError(f"unknown signature backend {backend!r}")


# ── helpers ─────────────────────────────────────────────────────────────────


def unsafe_name_reason(name: str) -> str | None:
    """Why *name* may not be written under an extraction root, or None if it may."""
    if not name or "\x00" in name or "\\" in name:
        return "empty, NUL or backslash in the name"
    path = PurePosixPath(name)
    if path.is_absolute():
        return "absolute path"
    if any(part in ("..", ".", "") for part in name.split("/")):
        return "'..', '.' or empty path component"
    return None


def parse_sha256sums(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse ``sha256sum`` output (text or ``*`` binary mode) -> ({path: hex}, errors)."""
    entries: dict[str, str] = {}
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        digest, sep, rest = line.partition(" ")
        if not sep or not _HEX64.match(digest) or rest[:1] not in (" ", "*"):
            errors.append(f"{SUMS_NAME} line {lineno} is not '<sha256>  <path>'")
            continue
        path = rest[1:]
        if path in entries:
            errors.append(f"{SUMS_NAME} lists {path!r} twice")
        entries[path] = digest
    return entries, errors


def _read_small(tar: tarfile.TarFile, info: tarfile.TarInfo, limit: int = 64 << 20) -> bytes:
    if info.size > limit:
        raise ValueError(f"{info.name!r} is {info.size} bytes, over the {limit}-byte limit")
    fobj = tar.extractfile(info)
    if fobj is None:
        raise ValueError(f"{info.name!r} is not a regular file")
    return fobj.read()


def _check_manifest_shape(manifest: Any) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest is not a JSON object"]
    if manifest.get("kind") != MANIFEST_KIND:
        return [f"manifest kind is {manifest.get('kind')!r}, expected {MANIFEST_KIND!r}"]
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        return ["manifest lists no members"]
    errors: list[str] = []
    seen: set[str] = set()
    for i, m in enumerate(members):
        if not isinstance(m, dict):
            errors.append(f"manifest member #{i} is not an object")
            continue
        path, digest, size = m.get("path"), m.get("sha256"), m.get("size")
        if not isinstance(path, str):
            errors.append(f"manifest member #{i} has no path")
            continue
        reason = unsafe_name_reason(path)
        if reason:
            errors.append(f"manifest member {path!r} has an unsafe path ({reason})")
        if path in (MANIFEST_NAME, ENVELOPE_NAME):
            errors.append(f"manifest member {path!r} uses a reserved name")
        if path in seen:
            errors.append(f"manifest lists {path!r} twice")
        seen.add(path)
        if not isinstance(digest, str) or not _HEX64.match(digest):
            errors.append(f"manifest member {path!r} has no valid sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            errors.append(f"manifest member {path!r} has no valid size")
    return errors


# ── the verifier ────────────────────────────────────────────────────────────


def verify_bundle(
    bundle: Path,
    *,
    public_key_pem: bytes | None,
    extract_to: Path | None = None,
    backend: str = "auto",
) -> Report:
    """Verify *bundle*; every failure is collected and named (an auditor wants
    the whole list). ``public_key_pem=None`` means integrity only."""
    report = Report()
    staging: Path | None = None
    claim: tuple[int, int] | None = None
    completed = False
    try:
        if extract_to is not None:
            # Claim the target name exclusively up front: mkdir fails if anything
            # already sits there, so verified members can never land on (or merge
            # into) a directory someone else created.
            try:
                os.mkdir(extract_to, 0o700)
            except FileExistsError:
                report.errors.append(f"{extract_to} already exists; choose a new directory")
                return report
            st = os.lstat(extract_to)
            claim = (st.st_dev, st.st_ino)
        with tarfile.open(bundle, "r:*") as tar:
            infos = tar.getmembers()
            by_name: dict[str, tarfile.TarInfo] = {}
            for info in infos:
                if info.name in by_name:
                    report.errors.append(f"member {info.name!r} appears more than once in the tar")
                by_name[info.name] = info
                if not info.isfile():
                    report.errors.append(
                        f"member {info.name!r} is not a regular file (links, devices and "
                        "directories are refused)"
                    )
            for required in (MANIFEST_NAME, ENVELOPE_NAME):
                if required not in by_name:
                    report.errors.append(f"bundle is missing {required}")
            if report.errors:
                return report

            manifest_bytes = _read_small(tar, by_name[MANIFEST_NAME])
            try:
                envelope = json.loads(_read_small(tar, by_name[ENVELOPE_NAME]))
                payload_type = envelope["payloadType"]
                payload = base64.b64decode(envelope["payload"], validate=True)
                signatures = [
                    base64.b64decode(s["sig"], validate=True) for s in envelope["signatures"]
                ]
            except (ValueError, KeyError, TypeError, binascii.Error) as exc:
                report.errors.append(f"signature envelope is malformed: {exc}")
                return report
            if payload_type != PAYLOAD_TYPE:
                report.errors.append(
                    f"envelope payloadType is {payload_type!r}, expected {PAYLOAD_TYPE!r}"
                )
                return report

            # 1. Signature, before any byte of the manifest is trusted.
            if public_key_pem is None:
                report.warnings.append(
                    "signature NOT checked (--integrity-only): this detects corruption, "
                    "not a substituted bundle"
                )
            else:
                pae = dsse_pae(payload_type, payload)
                try:
                    valid = any(
                        verify_ed25519(public_key_pem, pae, sig, backend=backend)
                        for sig in signatures
                    )
                except SignatureUnavailable as exc:
                    report.errors.append(
                        f"cannot check the signature on this host ({exc}); the bundle is NOT "
                        "verified. Install OpenSSL 3, or pass --integrity-only knowingly"
                    )
                    return report
                except ValueError as exc:
                    report.errors.append(f"manifest signature invalid: {exc}")
                    return report
                if not valid:
                    report.errors.append(
                        "manifest signature invalid: no signature verifies with the given key"
                    )
                    return report
                report.signature_checked = True

            # 2. The manifest in the tar is the signed one, byte for byte.
            if payload != manifest_bytes:
                report.errors.append("manifest bytes do not match the signed payload")
                return report
            try:
                manifest = json.loads(manifest_bytes)
            except ValueError as exc:
                report.errors.append(f"manifest is not valid JSON: {exc}")
                return report
            shape = _check_manifest_shape(manifest)
            if shape:
                report.errors.extend(shape)
                return report
            listed = {m["path"]: m for m in manifest["members"]}

            # 3. Completeness both ways, then every byte.
            for extra in sorted(set(by_name) - set(listed) - {MANIFEST_NAME, ENVELOPE_NAME}):
                report.errors.append(f"member {extra!r} is present but not in the signed manifest")
            if extract_to is not None:
                staging = Path(tempfile.mkdtemp(prefix=".airgap-extract-", dir=extract_to.parent))
            for path, member in sorted(listed.items()):
                info = by_name.get(path)
                if info is None:
                    report.errors.append(f"member {path!r} is in the manifest but missing")
                    continue
                if info.size != member["size"]:
                    report.errors.append(
                        f"member {path!r} size mismatch: manifest {member['size']}, "
                        f"actual {info.size}"
                    )
                    continue
                digest = _hash_member(tar, info, None if staging is None else staging / path)
                if digest != member["sha256"]:
                    report.errors.append(
                        f"member {path!r} hash mismatch: manifest {member['sha256'][:12]}…, "
                        f"actual {digest[:12]}…"
                    )
                    continue
                report.members_verified += 1

            # 4. SHA256SUMS (closure bundles) must agree with the signed manifest.
            if SUMS_NAME in listed and SUMS_NAME in by_name and not report.errors:
                text = _read_small(tar, by_name[SUMS_NAME]).decode("utf-8", "replace")
                sums, sum_errors = parse_sha256sums(text)
                report.errors.extend(sum_errors)
                expected = {p: m["sha256"] for p, m in listed.items() if p != SUMS_NAME}
                for path in sorted(set(expected) | set(sums)):
                    if sums.get(path) != expected.get(path):
                        report.errors.append(
                            f"{SUMS_NAME} disagrees with the signed manifest for {path!r}"
                        )
        completed = True  # keep last: an exception or interrupt above leaves it False
    except (tarfile.TarError, OSError, ValueError) as exc:
        report.errors.append(f"cannot read the bundle: {exc}")
    finally:
        # Runs on every path, KeyboardInterrupt included: the target receives the
        # staging directory only when every member was written AND verified.
        if completed and report.ok and staging is not None and extract_to is not None:
            try:
                _replace_claim(staging, extract_to, claim)
                report.extracted_to = extract_to
            except OSError as exc:
                report.errors.append(f"cannot move the verified members into {extract_to}: {exc}")
        if report.extracted_to is None:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if extract_to is not None and claim is not None:
                _release_claim(extract_to, claim)
    return report


def _same_empty_dir(path: Path, claim: tuple[int, int] | None) -> bool:
    try:
        st = os.lstat(path)
        return (
            claim is not None
            and (st.st_dev, st.st_ino) == claim
            and stat.S_ISDIR(st.st_mode)
            and not os.listdir(path)
        )
    except OSError:
        return False


def _replace_claim(staging: Path, target: Path, claim: tuple[int, int] | None) -> None:
    """Rename *staging* over the empty directory this run claimed at *target*.

    POSIX rename replaces only an EMPTY directory, so a non-empty target fails;
    the identity check refuses a target that was swapped after the claim.
    """
    if not _same_empty_dir(target, claim):
        raise OSError(f"{target} changed during verification; refusing to replace it")
    os.rename(staging, target)


def _release_claim(target: Path, claim: tuple[int, int]) -> None:
    """Remove the claimed target only if it is still this run's empty directory."""
    if _same_empty_dir(target, claim):
        try:
            os.rmdir(target)
        except OSError:
            pass


def _hash_member(tar: tarfile.TarFile, info: tarfile.TarInfo, dest: Path | None) -> str:
    fobj = tar.extractfile(info)
    if fobj is None:  # pragma: no cover — non-regular members were refused above
        raise ValueError(f"{info.name!r} is not a regular file")
    h = hashlib.sha256()
    out = None
    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        out = open(dest, "xb")  # noqa: SIM115 — closed in finally
    try:
        for chunk in iter(lambda: fobj.read(_CHUNK), b""):
            h.update(chunk)
            if out is not None:
                out.write(chunk)
    finally:
        if out is not None:
            out.close()
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bundle", required=True, type=Path, help="the bundle tar")
    key = parser.add_mutually_exclusive_group(required=True)
    key.add_argument(
        "--public-key",
        type=Path,
        help="ed25519 public key (PEM) obtained out of band, not from the bundle",
    )
    key.add_argument(
        "--integrity-only",
        action="store_true",
        help="skip the signature check: detects corruption, NOT substitution",
    )
    parser.add_argument(
        "--extract-to",
        type=Path,
        help="write the verified members to this new directory (must not exist)",
    )
    args = parser.parse_args(argv)

    if not args.bundle.is_file():
        parser.error(f"--bundle {args.bundle} is not a file")
    if args.extract_to is not None:
        if os.path.lexists(args.extract_to):
            parser.error(f"--extract-to {args.extract_to} already exists; choose a new directory")
        if not args.extract_to.parent.is_dir():
            parser.error(f"--extract-to: parent {args.extract_to.parent} is not a directory")
    public_pem = None if args.integrity_only else args.public_key.read_bytes()

    report = verify_bundle(args.bundle, public_key_pem=public_pem, extract_to=args.extract_to)
    for warning in report.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if not report.ok:
        print(f"FAILED — {len(report.errors)} problem(s):", file=sys.stderr)
        for err in report.errors:
            print(f"  - {err}", file=sys.stderr)
        if args.extract_to is not None:
            print(f"nothing was written to {args.extract_to}", file=sys.stderr)
        return EXIT_FAILED
    what = (
        "signature valid" if report.signature_checked else "integrity only, signature NOT checked"
    )
    print(f"OK — {what}, {report.members_verified} members verified")
    if report.extracted_to is not None:
        print(f"extracted the verified members to {report.extracted_to}")
        if (report.extracted_to / "requirements.txt").is_file():
            root = report.extracted_to
            print(
                "install with: python3 -m pip install --no-index "
                f"--find-links {root / 'wheels'} --require-hashes -r {root / 'requirements.txt'}"
            )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
