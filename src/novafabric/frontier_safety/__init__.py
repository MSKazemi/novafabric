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

"""Runtime safety & alignment evidence (ADR-0167, experimental).

Record-only: NovaFabric records that an external frontier-safety-framework
evaluator, control protocol, or human decision-maker reached a conclusion. It
never runs a dangerous-capability evaluation, never computes a safety verdict,
and never enforces, blocks, or gates a workload.
"""

from novafabric.frontier_safety.facet import (
    FACET_NAME,
    MAX_REF_LENGTH,
    SCHEMA_VERSION,
    CommitmentBinding,
    ComputedVerdictError,
    FrontierSafetyError,
    FrontierSafetyFacet,
    InvalidReferenceError,
    PayloadCaptureError,
    ThresholdEval,
    VerificationFlags,
    attach_facet,
    build_facet,
    digest_ref,
    facet_from_capsule,
    verify_commitment_binding,
    verify_eval_binding,
)

__all__ = [
    "FACET_NAME",
    "MAX_REF_LENGTH",
    "SCHEMA_VERSION",
    "CommitmentBinding",
    "ComputedVerdictError",
    "FrontierSafetyError",
    "FrontierSafetyFacet",
    "InvalidReferenceError",
    "PayloadCaptureError",
    "ThresholdEval",
    "VerificationFlags",
    "attach_facet",
    "build_facet",
    "digest_ref",
    "facet_from_capsule",
    "verify_commitment_binding",
    "verify_eval_binding",
]
