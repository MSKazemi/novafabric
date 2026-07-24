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

"""Training & model-provenance lineage (ADR-0152, experimental).

Record-only: NovaFabric binds references to the artifacts a model was built
from. It never trains, fine-tunes, distills, aligns or serves a model, and it
never captures training data, weights, or preference labels.
"""

from novafabric.provenance.model_provenance import (
    FACET_NAME,
    MAX_CHAIN_HOPS,
    MAX_REF_LENGTH,
    SCHEMA_VERSION,
    ChainTooLargeError,
    CheckpointChainError,
    CheckpointHop,
    CheckpointStage,
    CyclicCheckpointChainError,
    DataCardRef,
    DuplicateCheckpointError,
    FingerprintCheck,
    FingerprintScheme,
    FingerprintStatus,
    InvalidReferenceError,
    ModelProvenanceError,
    ModelProvenanceFacet,
    PayloadCaptureError,
    UnresolvedParentError,
    VerificationFlags,
    WeightCaptureError,
    WeightFingerprint,
    attach_facet,
    build_chain,
    build_facet,
    chain_head,
    check_fingerprint,
    data_card_ref,
    digest_ref,
    facet_from_capsule,
    hop_preimage,
    pin_fingerprint,
    record_fingerprint_check,
    scan_for_payloads,
    unbound_card_refs,
    verify_artifact_ref,
    verify_chain,
    verify_chain_binding,
)

__all__ = [
    "FACET_NAME",
    "MAX_CHAIN_HOPS",
    "MAX_REF_LENGTH",
    "SCHEMA_VERSION",
    "ChainTooLargeError",
    "CheckpointChainError",
    "CheckpointHop",
    "CheckpointStage",
    "CyclicCheckpointChainError",
    "DataCardRef",
    "DuplicateCheckpointError",
    "FingerprintCheck",
    "FingerprintScheme",
    "FingerprintStatus",
    "InvalidReferenceError",
    "ModelProvenanceError",
    "ModelProvenanceFacet",
    "PayloadCaptureError",
    "UnresolvedParentError",
    "VerificationFlags",
    "WeightCaptureError",
    "WeightFingerprint",
    "attach_facet",
    "build_chain",
    "build_facet",
    "chain_head",
    "check_fingerprint",
    "data_card_ref",
    "digest_ref",
    "facet_from_capsule",
    "hop_preimage",
    "pin_fingerprint",
    "record_fingerprint_check",
    "scan_for_payloads",
    "unbound_card_refs",
    "verify_artifact_ref",
    "verify_chain",
    "verify_chain_binding",
]
