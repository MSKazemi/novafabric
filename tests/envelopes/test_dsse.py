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

"""Tests for the DSSE Evidence-Bundle envelope (NF-029, ADR-0096)."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.envelopes.dsse import (
    BUNDLE_PAYLOAD_TYPE,
    unwrap_bundle,
    verify_bundle_envelope,
    wrap_bundle,
)
from novafabric.evidence.intoto import dsse_pae

_BUNDLE = b'{"bundle_id":"01HXAY7M5JZ8R7K4P9DPBYK2WX","artifacts":[],"schema_version":"0.1.0"}'


class _Signer:
    def __init__(self, key: Ed25519PrivateKey, keyid: str) -> None:
        self._key = key
        self.keyid = keyid

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)


def _verify_fn(key: Ed25519PrivateKey):
    pub = key.public_key()

    def _f(pae: bytes, sig: bytes) -> bool:
        try:
            pub.verify(sig, pae)
            return True
        except Exception:
            return False

    return _f


def test_wrap_sets_payload_type_and_preserves_bytes() -> None:
    key = Ed25519PrivateKey.generate()
    env = wrap_bundle(_BUNDLE, _Signer(key, "novaseal:2026-07"))
    assert env["payloadType"] == BUNDLE_PAYLOAD_TYPE
    # inner bytes preserved verbatim (wrap, don't replace)
    assert base64.b64decode(env["payload"]) == _BUNDLE
    assert env["signatures"][0]["keyid"] == "novaseal:2026-07"


def test_round_trip_verifies() -> None:
    key = Ed25519PrivateKey.generate()
    env = wrap_bundle(_BUNDLE, _Signer(key, "k"))
    assert verify_bundle_envelope(env, _verify_fn(key)) == _BUNDLE


def test_unwrap_returns_bytes() -> None:
    key = Ed25519PrivateKey.generate()
    env = wrap_bundle(_BUNDLE, _Signer(key, "k"))
    assert unwrap_bundle(env) == _BUNDLE


def test_tampered_payload_fails_verification() -> None:
    key = Ed25519PrivateKey.generate()
    env = wrap_bundle(_BUNDLE, _Signer(key, "k"))
    tampered = dict(env)
    tampered["payload"] = base64.b64encode(b'{"bundle_id":"EVIL","artifacts":[]}').decode()
    with pytest.raises(ValueError, match="signature verification failed"):
        verify_bundle_envelope(tampered, _verify_fn(key))


def test_wrong_key_fails_verification() -> None:
    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    env = wrap_bundle(_BUNDLE, _Signer(key, "k"))
    with pytest.raises(ValueError):
        verify_bundle_envelope(env, _verify_fn(other))


def test_pae_matches_dsse_spec() -> None:
    # The signed bytes are the DSSE PAE over the bundle payload + type.
    key = Ed25519PrivateKey.generate()
    env = wrap_bundle(_BUNDLE, _Signer(key, "k"))
    expected_pae = dsse_pae(BUNDLE_PAYLOAD_TYPE, _BUNDLE)
    sig = base64.b64decode(env["signatures"][0]["sig"])
    key.public_key().verify(sig, expected_pae)  # raises if PAE differs
