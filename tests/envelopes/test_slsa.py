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

"""Tests for SLSA v1 promotion provenance (NF-031, ADR-0096)."""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.envelopes.slsa import (
    PROMOTE_BUILD_TYPE,
    SLSA_PREDICATE_TYPE,
    promotion_provenance,
)
from novafabric.evidence.intoto import INTOTO_STATEMENT_TYPE, dsse_sign, dsse_verify

_SHA = "42a3b7" + "0" * 58


class _Signer:
    def __init__(self, key: Ed25519PrivateKey, keyid: str) -> None:
        self._key = key
        self.keyid = keyid

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)


def test_statement_shape() -> None:
    stmt = promotion_provenance(asset_ref="agent:summarizer@1.1.0", asset_sha256=_SHA)
    assert stmt["_type"] == INTOTO_STATEMENT_TYPE
    assert stmt["predicateType"] == SLSA_PREDICATE_TYPE
    assert stmt["subject"][0]["digest"]["sha256"] == _SHA
    assert stmt["predicate"]["buildDefinition"]["buildType"] == PROMOTE_BUILD_TYPE
    assert stmt["predicate"]["buildDefinition"]["externalParameters"]["asset"] == "agent:summarizer@1.1.0"


def test_captures_closure() -> None:
    stmt = promotion_provenance(
        asset_ref="agent:x@1.0.0",
        asset_sha256="sha256:" + _SHA,
        eval_container="sha256:abc123",
        seeds=[1781162195],
        datasets=[{"uri": "dataset:gaia@2026-05", "sha256": "sha256:deadbeef"}],
        gate="regression-gate/v1",
        invocation_id="01HXBM1Y3K2NGH9V0RD9P0ZDC4",
    )
    bd = stmt["predicate"]["buildDefinition"]
    assert bd["internalParameters"]["evalContainer"] == "sha256:abc123"
    assert bd["internalParameters"]["seeds"] == [1781162195]
    assert bd["resolvedDependencies"][0]["uri"] == "dataset:gaia@2026-05"
    assert bd["resolvedDependencies"][0]["digest"]["sha256"] == "deadbeef"  # prefix stripped
    # subject sha256 prefix also stripped
    assert stmt["subject"][0]["digest"]["sha256"] == _SHA
    rd = stmt["predicate"]["runDetails"]
    assert rd["metadata"]["invocationId"] == "01HXBM1Y3K2NGH9V0RD9P0ZDC4"
    contents = {b["name"]: b["content"] for b in rd["byproducts"]}
    assert contents["promotion-decision"] == "promoted"
    assert contents["promotion-gate"] == "regression-gate/v1"


def test_minimal_omits_optional_fields() -> None:
    stmt = promotion_provenance(asset_ref="a@1", asset_sha256=_SHA)
    bd = stmt["predicate"]["buildDefinition"]
    assert bd["internalParameters"] == {}
    assert bd["resolvedDependencies"] == []
    rd = stmt["predicate"]["runDetails"]
    assert rd["metadata"] == {}
    assert [b["name"] for b in rd["byproducts"]] == ["promotion-decision"]


def test_custom_decision_and_builder() -> None:
    stmt = promotion_provenance(
        asset_ref="a@1", asset_sha256=_SHA, decision="blocked", builder_id="https://ci/runner"
    )
    assert stmt["predicate"]["runDetails"]["builder"]["id"] == "https://ci/runner"
    assert stmt["predicate"]["runDetails"]["byproducts"][0]["content"] == "blocked"


def test_provenance_is_dsse_payload_roundtrip() -> None:
    key = Ed25519PrivateKey.generate()
    stmt = promotion_provenance(asset_ref="a@1", asset_sha256=_SHA)
    env = dsse_sign(stmt, _Signer(key, "k"))
    pub = key.public_key()

    def _verify(pae: bytes, sig: bytes) -> bool:
        try:
            pub.verify(sig, pae)
            return True
        except Exception:
            return False

    assert dsse_verify(env, _verify) == stmt


# ── NF-057 SLSA-for-ML profile ───────────────────────────────────────────────


def test_ml_profile_build_type_and_verdict() -> None:
    from novafabric.envelopes.schema import validate_slsa_provenance
    from novafabric.envelopes.slsa import PROMOTE_ML_BUILD_TYPE, ml_promotion_provenance

    stmt = ml_promotion_provenance(
        asset_ref="model:summarizer@1.1.0",
        asset_sha256=_SHA,
        eval_verdict_sha256="a1b2" + "0" * 60,
        seeds=[1781162195],
        eval_container="sha256:7c9e" + "0" * 60,
        datasets=[{"uri": "dataset:gaia@2026-05", "sha256": "b17a" + "0" * 60}],
        gate="regression-gate/v1",
        invocation_id="01HXBM1Y3K2NGH9V0RD9P0ZDC4",
    )
    bd = stmt["predicate"]["buildDefinition"]
    assert bd["buildType"] == PROMOTE_ML_BUILD_TYPE
    assert bd["internalParameters"]["seeds"] == [1781162195]
    assert bd["resolvedDependencies"][0]["uri"] == "dataset:gaia@2026-05"
    byproducts = {b["name"]: b for b in stmt["predicate"]["runDetails"]["byproducts"]}
    assert byproducts["gate-rule"]["content"] == "regression-gate/v1"
    assert byproducts["eval-verdict"]["digest"]["sha256"] == "a1b2" + "0" * 60
    # Still a valid SLSA v1 provenance Statement.
    validate_slsa_provenance(stmt)


def test_ml_profile_dsse_roundtrip() -> None:
    from novafabric.envelopes.slsa import ml_promotion_provenance

    stmt = ml_promotion_provenance(
        asset_ref="model:m@1", asset_sha256=_SHA, eval_verdict_sha256="c3d4" + "0" * 60
    )
    key = Ed25519PrivateKey.generate()
    env = dsse_sign(stmt, _Signer(key, "k1"))
    pub = key.public_key()

    def _verify(pae: bytes, sig: bytes) -> bool:
        try:
            pub.verify(sig, pae)
            return True
        except Exception:
            return False

    assert dsse_verify(env, _verify) == stmt


def test_generic_provenance_unchanged_by_default() -> None:
    """NF-031 default output has no eval-verdict byproduct (backward compatible)."""
    stmt = promotion_provenance(asset_ref="a@1", asset_sha256=_SHA, gate="g")
    names = {b["name"] for b in stmt["predicate"]["runDetails"]["byproducts"]}
    assert "eval-verdict" not in names
    assert "promotion-gate" in names  # generic gate byproduct name unchanged
