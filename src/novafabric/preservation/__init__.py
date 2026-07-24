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

"""Evidence longevity & long-term preservation (ADR-0165, experimental).

Record-only: NovaFabric records that preservation happened — a fixity check
ran, a custody hop occurred, a seal was renewed. It does not provide archival
storage, run a Timestamp Authority, convert formats, generate keys, repair bit
rot, or guarantee that any archive is durable, lawful, or regulator-accepted.

P1 ships the NF-331 anchor and the NF-335 fixity log only. The format-migration
chain (NF-332), crypto re-seal and LTV renewal (NF-333/334), conformance,
obsolescence and custody (NF-336/337/338), and the whole-chain re-verification
receipt (NF-339/340) are later slices.
"""

from novafabric.preservation.anchor import (
    FACET_NAME,
    MAX_REF_LENGTH,
    SCHEMA_VERSION,
    Fixity,
    FixityAlg,
    FixityCheck,
    FixityLogRewriteError,
    FixityStatus,
    InvalidDigestError,
    PayloadCaptureError,
    PreservationError,
    PreservationFacet,
    ProvenanceEvent,
    append_fixity_check,
    append_provenance_event,
    attach_facet,
    build_anchor,
    check_fixity,
    detected_bit_rot,
    digest_artifact,
    facet_from_capsule,
    fixity_status,
    provenance_event,
    scan_for_payloads,
    verify_anchor_binding,
    verify_append_only,
)

__all__ = [
    "FACET_NAME",
    "MAX_REF_LENGTH",
    "SCHEMA_VERSION",
    "Fixity",
    "FixityAlg",
    "FixityCheck",
    "FixityLogRewriteError",
    "FixityStatus",
    "InvalidDigestError",
    "PayloadCaptureError",
    "PreservationError",
    "PreservationFacet",
    "ProvenanceEvent",
    "append_fixity_check",
    "append_provenance_event",
    "attach_facet",
    "build_anchor",
    "check_fixity",
    "detected_bit_rot",
    "digest_artifact",
    "facet_from_capsule",
    "fixity_status",
    "provenance_event",
    "scan_for_payloads",
    "verify_anchor_binding",
    "verify_append_only",
]
