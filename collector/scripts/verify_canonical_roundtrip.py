#!/usr/bin/env python3
"""
Cross-language canonical byte round-trip verifier.

Reads a JSON manifest produced by TestCanonicalEncode_CrossLanguageRoundTrip10K
and verifies each Ed25519 signature using Python's cryptography library.

This script satisfies BQ-011 criterion: "Canonical byte encoding passes
cross-language round-trip test on 10K-batch corpus."

Usage:
    python3 verify_canonical_roundtrip.py <manifest.json>

Exit codes:
    0  all signatures verified
    1  one or more signature failures or usage error
"""
import base64
import json
import sys


def main(manifest_path: str) -> int:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        print(
            "ERROR: 'cryptography' package not installed. "
            "Install with: pip install cryptography",
            file=sys.stderr,
        )
        return 1

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read manifest {manifest_path!r}: {exc}", file=sys.stderr)
        return 1

    pub_bytes = bytes.fromhex(manifest["public_key_hex"])
    pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)

    events = manifest["events"]
    failures = 0
    for i, entry in enumerate(events):
        canonical = base64.b64decode(entry["canonical_b64"])
        sig = base64.b64decode(entry["signature_b64"])
        try:
            pub_key.verify(sig, canonical)
        except InvalidSignature as exc:
            print(f"FAIL event[{i}]: invalid signature — {exc}", file=sys.stderr)
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL event[{i}]: unexpected error — {exc}", file=sys.stderr)
            failures += 1

    total = len(events)
    if failures:
        print(
            f"FAIL: {failures}/{total} events failed signature verification",
            file=sys.stderr,
        )
        return 1

    print(f"OK: all {total} events verified (Ed25519 cross-language round-trip)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <manifest.json>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
