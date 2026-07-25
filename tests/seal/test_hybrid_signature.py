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
"""ADR-0072 Phase 1 — crypto-agility hybrid-signature envelope.

The migration story is *hybrid*: sign a payload under both a classical algorithm
(Ed25519 today) and a post-quantum one (ML-DSA when its library lands), carry both
in one envelope, and verify under whatever algorithms the verifier recognizes.
This tests the format + the pluggable registry + the ``any``/``all`` policies —
the algorithm-agility core that is unchanged when a PQC signer is added.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.trust.novaseal.hybrid_signature import (
    HybridSignatureEnvelope,
    register_algorithm,
    sign_hybrid,
    verify_hybrid,
)

PAYLOAD = b"capsule digest to protect across the quantum transition"


def _ed_signer() -> tuple[str, Ed25519PrivateKey, bytes]:
    k = Ed25519PrivateKey.generate()
    return "ed25519", k, k.public_key().public_bytes_raw()


class TestSingleAlgorithm:
    def test_ed25519_round_trip(self) -> None:
        env = sign_hybrid(PAYLOAD, [_ed_signer()])
        result = verify_hybrid(PAYLOAD, env)
        assert result.valid is True
        assert result.verified_algorithms == ["ed25519"]

    def test_tampered_payload_fails(self) -> None:
        env = sign_hybrid(PAYLOAD, [_ed_signer()])
        assert verify_hybrid(b"different payload", env).valid is False


class TestHybrid:
    def _register_fake_pqc(self) -> None:
        # A stand-in second algorithm (ML-DSA lands later); implemented over Ed25519
        # so the agility layer can be exercised end-to-end today.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        def _sign(key: Ed25519PrivateKey, payload: bytes) -> bytes:
            return key.sign(payload)

        def _verify(pub: bytes, sig: bytes, payload: bytes) -> bool:
            from cryptography.exceptions import InvalidSignature
            try:
                Ed25519PublicKey.from_public_bytes(pub).verify(sig, payload)
                return True
            except InvalidSignature:
                return False

        register_algorithm("ml-dsa-test", _sign, _verify)

    def _pqc_signer(self) -> tuple[str, Ed25519PrivateKey, bytes]:
        k = Ed25519PrivateKey.generate()
        return "ml-dsa-test", k, k.public_key().public_bytes_raw()

    def test_two_algorithms_both_verify(self) -> None:
        self._register_fake_pqc()
        env = sign_hybrid(PAYLOAD, [_ed_signer(), self._pqc_signer()])
        result = verify_hybrid(PAYLOAD, env, policy="all_recognized")
        assert result.valid is True
        assert set(result.verified_algorithms) == {"ed25519", "ml-dsa-test"}

    def test_either_alone_is_sufficient_under_any_policy(self) -> None:
        # ADR-0072 Phase 1: "either alone is sufficient." Corrupt one signature; the
        # envelope still verifies under the 'any_recognized' policy.
        self._register_fake_pqc()
        env = sign_hybrid(PAYLOAD, [_ed_signer(), self._pqc_signer()])
        sigs = list(env.signatures)
        sigs[0] = sigs[0].model_copy(update={"signature": b"\x00" * 64})
        broken = HybridSignatureEnvelope(payload_digest=env.payload_digest, signatures=sigs)
        assert verify_hybrid(PAYLOAD, broken, policy="any_recognized").valid is True
        assert verify_hybrid(PAYLOAD, broken, policy="all_recognized").valid is False


class TestForwardCompatibility:
    def test_unrecognized_algorithm_is_skipped_not_fatal(self) -> None:
        # A future PQC algorithm the verifier does not know is reported, not fatal:
        # the recognized (ed25519) signature still carries the envelope.
        env = sign_hybrid(PAYLOAD, [_ed_signer()])
        future = env.signatures[0].model_copy(
            update={"algorithm": "ml-dsa-87-future", "signature": b"\x01" * 32}
        )
        mixed = HybridSignatureEnvelope(
            payload_digest=env.payload_digest, signatures=[env.signatures[0], future]
        )
        result = verify_hybrid(PAYLOAD, mixed, policy="any_recognized")
        assert result.valid is True
        assert "ml-dsa-87-future" in result.unrecognized_algorithms

    def test_only_unrecognized_is_invalid(self) -> None:
        env = sign_hybrid(PAYLOAD, [_ed_signer()])
        future_only = HybridSignatureEnvelope(
            payload_digest=env.payload_digest,
            signatures=[env.signatures[0].model_copy(update={"algorithm": "unknown-pqc"})],
        )
        result = verify_hybrid(PAYLOAD, future_only)
        assert result.valid is False  # no recognized signature verified

    def test_required_algorithm_enforced(self) -> None:
        # A deployment can demand a specific algorithm be present and valid.
        env = sign_hybrid(PAYLOAD, [_ed_signer()])
        assert verify_hybrid(PAYLOAD, env, required_algorithms={"ed25519"}).valid is True
        assert verify_hybrid(PAYLOAD, env, required_algorithms={"ml-dsa-65"}).valid is False


def test_sign_hybrid_requires_at_least_one_signer() -> None:
    with pytest.raises(ValueError):
        sign_hybrid(PAYLOAD, [])
