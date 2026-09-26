# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic golden fixtures for NF-363 trust paths (ADR-0168 P2).

Keys are **test-only** Ed25519 keys derived from fixed, public seeds, so the
signatures (Ed25519 is deterministic) and hence the fixture JSON are
byte-reproducible. ``test_trust_path.py`` regenerates every fixture and asserts
the checked-in file is identical, so a change to the statement encoding cannot
land without the fixtures moving with it.

Regenerate after an intentional encoding change::

    .venv/bin/python -m tests.federation._trust_path_fixtures
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.federation.trust_path import (
    TrustPathHop,
    key_digest,
    sign_hop,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "federation" / "trust-path"


def fixture_key(name: str) -> Ed25519PrivateKey:
    """A deterministic, public, test-only Ed25519 key (seed = sha256-free byte fill)."""
    seed = (name.encode("ascii") * 32)[:32]
    return Ed25519PrivateKey.from_private_bytes(seed)


def spki_b64(key: Ed25519PrivateKey) -> str:
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode("ascii")


A, B, C, X, EVIL = (fixture_key(n) for n in ("orgA", "orgB", "orgC", "orgX", "evil"))
ANCHOR_DIGEST = key_digest(A.public_key())
EVIL_DIGEST = key_digest(EVIL.public_key())


def hop_dict(hop: TrustPathHop) -> dict[str, Any]:
    return hop.model_dump(exclude_none=True)


def valid_path(*, max0: int | None = None, max1: int | None = None) -> list[dict[str, Any]]:
    """A → B → C, rooted at the pinned anchor orgA."""
    h0 = sign_hop(
        A,
        from_org="orgA",
        to_org="orgB",
        subject_public_key=B.public_key(),
        anchor_digest=ANCHOR_DIGEST,
        max_path_length=max0,
    )
    h1 = sign_hop(
        B,
        from_org="orgB",
        to_org="orgC",
        subject_public_key=C.public_key(),
        anchor_digest=ANCHOR_DIGEST,
        max_path_length=max1,
    )
    return [hop_dict(h0), hop_dict(h1)]


def _cases() -> dict[str, dict[str, Any]]:
    anchors = [{"org": "orgA", "public_key_spki_b64": spki_b64(A)}]
    valid = valid_path()

    wrong_key = list(valid)
    wrong_key[1] = hop_dict(
        sign_hop(
            X,  # not the key hop 0 vouched for
            from_org="orgB",
            to_org="orgC",
            subject_public_key=C.public_key(),
            anchor_digest=ANCHOR_DIGEST,
        )
    )

    cycle = [
        *valid,
        hop_dict(
            sign_hop(
                C,
                from_org="orgC",
                to_org="orgB",
                subject_public_key=B.public_key(),
                anchor_digest=ANCHOR_DIGEST,
            )
        ),
    ]

    unpinned = [
        hop_dict(
            sign_hop(
                EVIL,
                from_org="evil",
                to_org="orgB",
                subject_public_key=B.public_key(),
                anchor_digest=EVIL_DIGEST,
            )
        ),
        hop_dict(
            sign_hop(
                B,
                from_org="orgB",
                to_org="orgC",
                subject_public_key=C.public_key(),
                anchor_digest=EVIL_DIGEST,
            )
        ),
    ]

    substituted = [
        valid[0],
        hop_dict(
            sign_hop(
                B,
                from_org="orgB",
                to_org="orgC",
                subject_public_key=C.public_key(),
                anchor_digest=EVIL_DIGEST,
            )
        ),
    ]

    malformed_sig = [valid[0], {**valid[1], "signature": base64.b64encode(b"\x00" * 64).decode()}]

    return {
        "valid-a-b-c.json": {
            "description": "A -> B -> C rooted at the verifier-pinned anchor orgA.",
            "anchors": anchors,
            "trust_path": valid,
            "expect": {"path_walk_ok": True, "reason": None, "broken_hop": None},
        },
        "valid-depth-exceeded.json": {
            "description": "Hop 0 signs max_path_length=0 but a second hop follows: "
            "flagged delegation_depth_exceeded, not fatal.",
            "anchors": anchors,
            "trust_path": valid_path(max0=0),
            "expect": {
                "path_walk_ok": True,
                "reason": None,
                "broken_hop": None,
                "delegation_depth_exceeded": True,
            },
        },
        "invalid-wrong-key.json": {
            "description": "Hop 1 is signed by orgX, not the key hop 0 vouched for.",
            "anchors": anchors,
            "trust_path": wrong_key,
            "expect": {"path_walk_ok": False, "reason": "bad_signature", "broken_hop": 1},
        },
        "invalid-reordered.json": {
            "description": "Valid hops, swapped: the path no longer starts at the anchor.",
            "anchors": anchors,
            "trust_path": [valid[1], valid[0]],
            "expect": {
                "path_walk_ok": False,
                "reason": "anchor_org_mismatch",
                "broken_hop": 0,
            },
        },
        "invalid-cycle.json": {
            "description": "A -> B -> C -> B: C re-vouches for B's key.",
            "anchors": anchors,
            "trust_path": cycle,
            "expect": {"path_walk_ok": False, "reason": "cycle", "broken_hop": 2},
        },
        "invalid-unpinned-anchor.json": {
            "description": "A well-signed path rooted at an anchor the verifier never pinned.",
            "anchors": anchors,
            "trust_path": unpinned,
            "expect": {
                "path_walk_ok": False,
                "reason": "anchor_not_pinned",
                "broken_hop": 0,
            },
        },
        "invalid-anchor-substituted.json": {
            "description": "Hop 1 resolves toward a different anchor than hop 0.",
            "anchors": anchors,
            "trust_path": substituted,
            "expect": {"path_walk_ok": False, "reason": "anchor_mismatch", "broken_hop": 1},
        },
        "invalid-malformed-signature.json": {
            "description": "Hop 1's signature is well-formed base64 but not a signature.",
            "anchors": anchors,
            "trust_path": malformed_sig,
            "expect": {"path_walk_ok": False, "reason": "bad_signature", "broken_hop": 1},
        },
    }


def render(case: dict[str, Any]) -> str:
    """Deterministic on-disk form of a fixture."""
    return json.dumps(case, indent=2, sort_keys=True) + "\n"


def cases() -> dict[str, str]:
    """Every fixture file name → its exact expected content."""
    return {name: render(case) for name, case in _cases().items()}


if __name__ == "__main__":  # pragma: no cover - maintenance entry point
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in cases().items():
        (FIXTURE_DIR / name).write_text(text, encoding="utf-8")
