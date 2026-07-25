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
"""ADR-0077 — jurisdiction site-seals + residency policy.

Two load-bearing properties:

* **Cryptographic proof of residency.** A capsule claiming ``jurisdiction=EU`` must
  carry a site-seal made by the *EU* jurisdiction key. A capsule that claims EU but
  is sealed by a different jurisdiction's key is rejected — the residency claim
  cannot be forged.
* **Residency-respecting reads.** A cross-jurisdiction read is denied unless the
  federation policy explicitly grants that border.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.compliance.sovereignty import (
    ResidencyPolicy,
    check_cross_jurisdiction_read,
    issue_site_seal,
    verify_site_seal,
)

DIGEST = "sha256:" + "a" * 64


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _pub(k: Ed25519PrivateKey) -> bytes:
    return k.public_key().public_bytes_raw()


class TestSiteSeal:
    def test_valid_residency_seal_verifies(self) -> None:
        eu = _key()
        seal = issue_site_seal(eu, jurisdiction="EU", content_digest=DIGEST)
        assert verify_site_seal(seal, jurisdiction_pubkeys={"EU": _pub(eu)}) is True

    def test_forged_jurisdiction_claim_is_rejected(self) -> None:
        # Claims EU but sealed with the US key; verified against the real EU key → reject.
        us = _key()
        seal = issue_site_seal(us, jurisdiction="EU", content_digest=DIGEST)
        real_eu = _key()
        assert verify_site_seal(seal, jurisdiction_pubkeys={"EU": _pub(real_eu)}) is False

    def test_unknown_jurisdiction_is_rejected(self) -> None:
        eu = _key()
        seal = issue_site_seal(eu, jurisdiction="EU", content_digest=DIGEST)
        assert verify_site_seal(seal, jurisdiction_pubkeys={"US": _pub(_key())}) is False

    def test_tampered_digest_is_rejected(self) -> None:
        eu = _key()
        seal = issue_site_seal(eu, jurisdiction="EU", content_digest=DIGEST)
        bad = seal.model_copy(update={"content_digest": "sha256:" + "b" * 64})
        assert verify_site_seal(bad, jurisdiction_pubkeys={"EU": _pub(eu)}) is False


class TestResidencyPolicy:
    def test_same_jurisdiction_read_allowed(self) -> None:
        policy = ResidencyPolicy()
        assert check_cross_jurisdiction_read("EU", "EU", policy).allowed is True

    def test_cross_read_denied_by_default(self) -> None:
        policy = ResidencyPolicy()
        decision = check_cross_jurisdiction_read("EU", "US", policy)
        assert decision.allowed is False
        assert decision.cross_jurisdiction is True

    def test_cross_read_allowed_when_policy_grants(self) -> None:
        policy = ResidencyPolicy(allow_cross_read={"EU": ["CH"]})
        assert check_cross_jurisdiction_read("EU", "CH", policy).allowed is True
        assert check_cross_jurisdiction_read("EU", "US", policy).allowed is False

    def test_grant_is_directional(self) -> None:
        # EU permitting CH to read EU data does NOT permit EU to read CH data.
        policy = ResidencyPolicy(allow_cross_read={"EU": ["CH"]})
        assert check_cross_jurisdiction_read("CH", "EU", policy).allowed is False


def test_issue_and_verify_round_trip_multi_jurisdiction() -> None:
    eu, us = _key(), _key()
    pubs = {"EU": _pub(eu), "US": _pub(us)}
    eu_seal = issue_site_seal(eu, jurisdiction="EU", content_digest=DIGEST)
    us_seal = issue_site_seal(us, jurisdiction="US", content_digest=DIGEST)
    assert verify_site_seal(eu_seal, jurisdiction_pubkeys=pubs) is True
    assert verify_site_seal(us_seal, jurisdiction_pubkeys=pubs) is True
    # A US capsule cannot masquerade as EU even with both keys registered.
    forged = issue_site_seal(us, jurisdiction="EU", content_digest=DIGEST)
    assert verify_site_seal(forged, jurisdiction_pubkeys=pubs) is False


def test_issue_requires_a_jurisdiction() -> None:
    with pytest.raises(ValueError):
        issue_site_seal(_key(), jurisdiction="", content_digest=DIGEST)
