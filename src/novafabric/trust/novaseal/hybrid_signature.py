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
"""Crypto-agility hybrid-signature envelope (ADR-0072 Phase 1).

The post-quantum transition is *hybrid*: a payload is signed under both a
classical algorithm (Ed25519 today) and a post-quantum one (ML-DSA when its
library lands), and **either alone is sufficient** to verify — so the artifact
survives a break in either family. This module provides that envelope and the
**pluggable algorithm registry** that makes the transition additive:

* :func:`register_algorithm` adds a ``(sign, verify)`` pair under a name. Ed25519
  is registered by default; ML-DSA-65/87 register themselves once a Tier-A PQC
  library is available (ADR-0072 gates the algorithm, not this layer).
* :func:`sign_hybrid` produces one envelope carrying a signature per signer.
* :func:`verify_hybrid` verifies each signature under its registered algorithm and
  applies a policy (``any_recognized`` — either alone suffices, the Phase 1
  default; or ``all_recognized``), optionally requiring named algorithms be
  present and valid. **A signature whose algorithm the verifier does not recognize
  is reported, not fatal** — an old verifier keeps working when a new algorithm is
  added (forward compatibility).

No new dependency: the Ed25519 signer/verifier reuse the existing ``cryptography``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

# algorithm name -> (sign(private_key, payload) -> bytes, verify(pub, sig, payload) -> bool)
_Signer = Callable[[Any, bytes], bytes]
_Verifier = Callable[[bytes, bytes, bytes], bool]
_REGISTRY: dict[str, tuple[_Signer, _Verifier]] = {}


def register_algorithm(name: str, sign: _Signer, verify: _Verifier) -> None:
    """Register a signature algorithm's ``(sign, verify)`` pair under *name*."""
    _REGISTRY[name] = (sign, verify)


def registered_algorithms() -> frozenset[str]:
    """Return the set of currently registered algorithm names."""
    return frozenset(_REGISTRY)


# ---- Ed25519 (classical) registered by default -----------------------------

def _ed25519_sign(private_key: Any, payload: bytes) -> bytes:
    return bytes(private_key.sign(payload))


def _ed25519_verify(public_key: bytes, signature: bytes, payload: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
        return True
    except (InvalidSignature, ValueError):
        return False


register_algorithm("ed25519", _ed25519_sign, _ed25519_verify)


# ---- Models ----------------------------------------------------------------

class AlgorithmSignature(BaseModel):
    """One algorithm's signature over the payload, with its public key."""

    model_config = {"frozen": True}

    algorithm: str
    public_key: bytes
    signature: bytes


class HybridSignatureEnvelope(BaseModel):
    """A payload's signatures under one or more algorithms (ADR-0072 hybrid)."""

    payload_digest: str = Field(
        ..., description="'sha256:...' of the signed payload, for reference"
    )
    signatures: list[AlgorithmSignature]


class HybridVerifyResult(BaseModel):
    """Outcome of verifying a hybrid envelope."""

    model_config = {"frozen": True}

    valid: bool
    verified_algorithms: list[str] = Field(default_factory=list)
    failed_algorithms: list[str] = Field(default_factory=list)
    unrecognized_algorithms: list[str] = Field(default_factory=list)
    reason: str | None = None


# ---- Sign / verify ---------------------------------------------------------

def sign_hybrid(
    payload: bytes,
    signers: list[tuple[str, Any, bytes]],
) -> HybridSignatureEnvelope:
    """Sign *payload* under each signer, returning one hybrid envelope.

    Args:
        payload: the bytes to sign.
        signers: list of ``(algorithm_name, private_key, public_key_bytes)``.

    Raises:
        ValueError: no signers, or an unregistered algorithm.
    """
    if not signers:
        raise ValueError("sign_hybrid requires at least one signer")
    sigs: list[AlgorithmSignature] = []
    for algorithm, private_key, public_key in signers:
        entry = _REGISTRY.get(algorithm)
        if entry is None:
            raise ValueError(f"cannot sign with unregistered algorithm {algorithm!r}")
        sign_fn, _ = entry
        sigs.append(AlgorithmSignature(
            algorithm=algorithm,
            public_key=public_key,
            signature=sign_fn(private_key, payload),
        ))
    return HybridSignatureEnvelope(
        payload_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
        signatures=sigs,
    )


def verify_hybrid(
    payload: bytes,
    envelope: HybridSignatureEnvelope,
    *,
    policy: Literal["any_recognized", "all_recognized"] = "any_recognized",
    required_algorithms: set[str] | frozenset[str] | None = None,
) -> HybridVerifyResult:
    """Verify a hybrid envelope under the registered algorithms and a policy.

    * ``any_recognized`` (default): valid if **at least one** recognized signature
      verifies — the ADR-0072 Phase 1 "either alone is sufficient" rule.
    * ``all_recognized``: **every** recognized signature must verify.

    An unrecognized algorithm is recorded in ``unrecognized_algorithms`` and never
    causes failure by itself (forward compatibility). *required_algorithms*, if
    given, must all be present *and* verify.
    """
    verified: list[str] = []
    failed: list[str] = []
    unrecognized: list[str] = []

    for sig in envelope.signatures:
        entry = _REGISTRY.get(sig.algorithm)
        if entry is None:
            unrecognized.append(sig.algorithm)
            continue
        _, verify_fn = entry
        try:
            ok = verify_fn(bytes(sig.public_key), bytes(sig.signature), payload)
        except Exception:  # pragma: no cover - defensive: a verifier must never leak
            ok = False
        (verified if ok else failed).append(sig.algorithm)

    def _fail(reason: str) -> HybridVerifyResult:
        return HybridVerifyResult(
            valid=False, verified_algorithms=verified, failed_algorithms=failed,
            unrecognized_algorithms=unrecognized, reason=reason,
        )

    if required_algorithms:
        missing = {a for a in required_algorithms if a not in verified}
        if missing:
            return _fail(f"required algorithm(s) absent or invalid: {sorted(missing)}")

    if not verified:
        return _fail("no recognized signature verified")
    if policy == "all_recognized" and failed:
        return _fail(f"policy all_recognized: {failed} did not verify")

    return HybridVerifyResult(
        valid=True, verified_algorithms=verified, failed_algorithms=failed,
        unrecognized_algorithms=unrecognized,
    )


__all__ = [
    "AlgorithmSignature",
    "HybridSignatureEnvelope",
    "HybridVerifyResult",
    "register_algorithm",
    "registered_algorithms",
    "sign_hybrid",
    "verify_hybrid",
]
