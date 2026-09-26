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

"""Cross-org federation evidence — ADR-0168 P1 (NF-361/NF-362) + P2 (NF-363).

Records *that* a cross-org exchange happened and *which* foreign trust anchor
was pinned (:mod:`novafabric.federation.facet`), and walks a signed transitive
trust path to an anchor the **verifier** pins
(:mod:`novafabric.federation.trust_path`, experimental). It never adjudicates
whether a foreign org is trustworthy: a pin is an act, and a valid path proves
only that a chain of signed delegation statements composes.

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
from novafabric.federation.trust_path import (
    MAX_HOPS,
    PinnedAnchor,
    TrustPath,
    TrustPathError,
    TrustPathHop,
    TrustPathReport,
    attach_trust_path,
    key_digest,
    parse_trust_path,
    sign_hop,
    trust_path_from_capsule,
    verify_trust_path,
)

__all__ = [
    "FACET_NAME",
    "MAX_HOPS",
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
    "PinnedAnchor",
    "ReferenceState",
    "TrustAnchorPin",
    "TrustPath",
    "TrustPathError",
    "TrustPathHop",
    "TrustPathReport",
    "anchor_state",
    "attach_facet",
    "attach_trust_path",
    "build_exchange",
    "build_facet",
    "build_trust_anchor",
    "digest_artifact",
    "facet_from_capsule",
    "key_digest",
    "parse_trust_path",
    "reference_state",
    "scan_for_payload",
    "sign_hop",
    "trust_path_from_capsule",
    "verify_trust_path",
]
