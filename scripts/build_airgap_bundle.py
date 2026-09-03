#!/usr/bin/env python3
"""Build a signed air-gap bundle (ADR-0249 slice 1).

Assembles named files into one tar with a DSSE-signed manifest that
``scripts/verify_airgap_bundle.py`` (or ``verify_bundle``) checks fully
offline. Slice 1 bundles what you give it; the CI job that assembles the
full closure (wheel set, image, chart, docs, advisory snapshot) is the next
slice.

Usage:
    uv run python scripts/build_airgap_bundle.py \\
        --out dist/novafabric-airgap.tar \\
        --signing-key ~/.novafabric/keys/evidence.key \\
        --member wheels/novafabric.whl=dist/novafabric-0.100.1-py3-none-any.whl \\
        --member docs/index.html=web/dist/index.html
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--signing-key", required=True, type=Path)
    parser.add_argument(
        "--member",
        action="append",
        default=[],
        metavar="ARCNAME=SRC",
        help="repeatable: in-bundle path = source file",
    )
    args = parser.parse_args()

    members: dict[str, Path] = {}
    for spec in args.member:
        arcname, sep, src = spec.partition("=")
        if not sep or not arcname or not src:
            parser.error(f"--member must be ARCNAME=SRC, got {spec!r}")
        members[arcname] = Path(src)

    from novafabric.export_blob.airgap import AirgapBundleError, build_bundle

    try:
        out = build_bundle(
            args.out,
            members,
            signing_key=args.signing_key,
            nova_version=pkg_version("novafabric"),
        )
    except AirgapBundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {out} ({len(members)} members + signed manifest)")
    print("verify offline with: scripts/verify_airgap_bundle.py --public-key <pem>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
