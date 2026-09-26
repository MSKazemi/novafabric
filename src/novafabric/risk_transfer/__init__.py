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

"""Insurance, liability & actuarial evidence (ADR-0170, experimental).

Record-only: NovaFabric records evidence the risk-transfer layer consumes —
loss-relevant features and declared incident losses bound to a DFIR bundle
(P1, NF-381/382), a liability-attribution chain (NF-383), and SLA/warranty
breach and coverage-trigger comparisons against declared terms (NF-384/386).
It does not underwrite, price, rate, or bind a policy, adjudicate a claim,
pay out, decide coverage, or assign legal liability or fault. The evidence supports an insurer's,
adjuster's, actuary's, or court's determination; it is never that
determination.
"""

from novafabric.risk_transfer._guard import (
    DeterminationFieldRejectedError,
    InvalidDecimalError,
    UnboundedFieldError,
)
from novafabric.risk_transfer.actuarial import (
    FACET_NAME,
    SCHEMA_VERSION,
    ActuarialBlock,
    FloatAmountRejectedError,
    IncidentLoss,
    InvalidReferenceError,
    LossFeature,
    LossFeatureKind,
    LossItem,
    LossSource,
    MissingDeclaredByError,
    MissingIncidentBundleError,
    Money,
    PaymentSecretRejectedError,
    RiskTransferFacet,
    UnquantifiedFeatureError,
    attach_facet,
    build_actuarial,
    build_facet,
    build_incident_loss,
    digest_artifact,
    extract_loss_features,
    is_measured,
    verify_ref_binding,
)
from novafabric.risk_transfer.liability import (
    InvalidLiabilityChainError,
    LiabilityEdge,
    UnsourcedContributionError,
    attributed_parties,
    build_liability_chain,
)
from novafabric.risk_transfer.signals import (
    CoverageTrigger,
    DeclaredExclusion,
    InconsistentComparisonError,
    ParametricTerm,
    SlaBreach,
    TriggerFact,
    build_coverage_trigger,
    build_sla_breach,
    compare,
)

__all__ = [
    "FACET_NAME",
    "SCHEMA_VERSION",
    "ActuarialBlock",
    "CoverageTrigger",
    "DeclaredExclusion",
    "DeterminationFieldRejectedError",
    "FloatAmountRejectedError",
    "IncidentLoss",
    "InconsistentComparisonError",
    "InvalidDecimalError",
    "InvalidLiabilityChainError",
    "InvalidReferenceError",
    "LiabilityEdge",
    "LossFeature",
    "LossFeatureKind",
    "LossItem",
    "LossSource",
    "MissingDeclaredByError",
    "MissingIncidentBundleError",
    "Money",
    "ParametricTerm",
    "PaymentSecretRejectedError",
    "RiskTransferFacet",
    "SlaBreach",
    "TriggerFact",
    "UnboundedFieldError",
    "UnquantifiedFeatureError",
    "UnsourcedContributionError",
    "attach_facet",
    "attributed_parties",
    "build_actuarial",
    "build_coverage_trigger",
    "build_facet",
    "build_incident_loss",
    "build_liability_chain",
    "build_sla_breach",
    "compare",
    "digest_artifact",
    "extract_loss_features",
    "is_measured",
    "verify_ref_binding",
]
