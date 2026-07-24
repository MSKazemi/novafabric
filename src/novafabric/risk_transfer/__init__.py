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
loss-relevant features and declared incident losses bound to a DFIR bundle.
It does not underwrite, price, rate, or bind a policy, adjudicate a claim,
pay out, or assign legal liability. The evidence supports an insurer's,
adjuster's, actuary's, or court's determination; it is never that
determination.
"""

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

__all__ = [
    "FACET_NAME",
    "SCHEMA_VERSION",
    "ActuarialBlock",
    "FloatAmountRejectedError",
    "IncidentLoss",
    "InvalidReferenceError",
    "LossFeature",
    "LossFeatureKind",
    "LossItem",
    "LossSource",
    "MissingDeclaredByError",
    "MissingIncidentBundleError",
    "Money",
    "PaymentSecretRejectedError",
    "RiskTransferFacet",
    "UnquantifiedFeatureError",
    "attach_facet",
    "build_actuarial",
    "build_facet",
    "build_incident_loss",
    "digest_artifact",
    "extract_loss_features",
    "is_measured",
    "verify_ref_binding",
]
