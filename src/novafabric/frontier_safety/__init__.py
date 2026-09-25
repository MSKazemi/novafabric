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
never runs a dangerous-capability evaluation or an AI-control protocol, never
computes a safety verdict, and never enforces, blocks, or gates a workload.

Shipped (experimental): P1 — the NF-351 threshold-eval and NF-353
commitment bindings (:mod:`.facet`); P2 — the NF-352 control-protocol decision
and NF-357 tripwire trigger (:mod:`.control`) with fail-open capture helpers
(:mod:`.record`); P3 — the NF-354 deception signal, NF-355 sandbagging record,
NF-356 autonomy attempt and NF-358 elicitation record (:mod:`.alignment`), each
an external finding held by reference.
"""

from novafabric.frontier_safety._common import (
    IN_MISSION_BOUNDARY,
    GuardrailDuplicationError,
)
from novafabric.frontier_safety.alignment import (
    AutonomyAttempt,
    DeceptionSignal,
    ElicitationRecord,
    SandbaggingRecord,
    total_attempts,
)
from novafabric.frontier_safety.control import (
    ControlDecision,
    ControlOutcome,
    ControlProtocol,
    TripwireTrigger,
    decisions_for_action,
    guardrail_decision_digest,
    verify_governed_action,
    verify_guardrail_link,
    verify_tripwire_commitment,
)
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
from novafabric.frontier_safety.record import (
    record_autonomy_attempt,
    record_control_decision,
    record_deception_signal,
    record_elicitation_record,
    record_sandbagging_record,
    record_tripwire_trigger,
)

__all__ = [
    "FACET_NAME",
    "IN_MISSION_BOUNDARY",
    "MAX_REF_LENGTH",
    "SCHEMA_VERSION",
    "AutonomyAttempt",
    "CommitmentBinding",
    "ComputedVerdictError",
    "ControlDecision",
    "ControlOutcome",
    "ControlProtocol",
    "DeceptionSignal",
    "ElicitationRecord",
    "FrontierSafetyError",
    "FrontierSafetyFacet",
    "GuardrailDuplicationError",
    "InvalidReferenceError",
    "PayloadCaptureError",
    "SandbaggingRecord",
    "ThresholdEval",
    "TripwireTrigger",
    "VerificationFlags",
    "attach_facet",
    "build_facet",
    "decisions_for_action",
    "digest_ref",
    "facet_from_capsule",
    "guardrail_decision_digest",
    "record_autonomy_attempt",
    "record_control_decision",
    "record_deception_signal",
    "record_elicitation_record",
    "record_sandbagging_record",
    "record_tripwire_trigger",
    "total_attempts",
    "verify_commitment_binding",
    "verify_eval_binding",
    "verify_governed_action",
    "verify_guardrail_link",
    "verify_tripwire_commitment",
]
