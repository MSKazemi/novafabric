#!/usr/bin/env python3
"""Verify an air-gap bundle fully offline (ADR-0249 slice 1).

Checks the DSSE signature over the manifest with the given ed25519 public
key, then every member's SHA-256 — naming each failure (a tampered member,
a missing one, or an unsigned stowaway). Zero network.

Usage:
    python scripts/verify_airgap_bundle.py \\
        --bundle novafabric-airgap.tar --public-key evidence.pub
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    args = parser.parse_args()

    from novafabric.export_blob.airgap import verify_bundle

    result = verify_bundle(args.bundle, public_key_pem=args.public_key.read_bytes())
    if result.ok:
        print(f"OK — signature valid, {result.members_verified} members verified")
        return 0
    print(f"FAILED — {len(result.errors)} problem(s):", file=sys.stderr)
    for err in result.errors:
        print(f"  - {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
