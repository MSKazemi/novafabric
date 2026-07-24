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

"""Cross-org federation evidence — ADR-0168 P1 (NF-361/NF-362).

Records *that* a cross-org exchange happened and *which* foreign trust anchor
was pinned. It never adjudicates whether a foreign org is trustworthy, and
never composes one pin into a conclusion about a third org — see
:mod:`novafabric.federation.facet` for both invariants and why they are
enforced by the shape of the API rather than by documentation alone.

Distinct from :mod:`novafabric.lineage.federation`, which fans lineage queries
out across *sites of one organisation*. That package is a query transport
inside a trust boundary; this one is evidence about crossing between them.
"""

from __future__ import annotations

from novafabric.federation.facet import (
    FACET_NAME,
    MAX_REF_LENGTH,
    SCHEMA_VERSION,
    AnchorState,
    EndpointProfile,
    ExchangeManifest,
    FederationError,
    FederationFacet,
    FederationVerification,
    InvalidReferenceError,
    PayloadCrossedBoundaryError,
    ReferenceState,
    TrustAnchorPin,
    anchor_state,
    attach_facet,
    build_exchange,
    build_facet,
    build_trust_anchor,
    digest_artifact,
    facet_from_capsule,
    reference_state,
    scan_for_payload,
)

__all__ = [
    "FACET_NAME",
    "MAX_REF_LENGTH",
    "SCHEMA_VERSION",
    "AnchorState",
    "EndpointProfile",
    "ExchangeManifest",
    "FederationError",
    "FederationFacet",
    "FederationVerification",
    "InvalidReferenceError",
    "PayloadCrossedBoundaryError",
    "ReferenceState",
    "TrustAnchorPin",
    "anchor_state",
    "attach_facet",
    "build_exchange",
    "build_facet",
    "build_trust_anchor",
    "digest_artifact",
    "facet_from_capsule",
    "reference_state",
    "scan_for_payload",
]
