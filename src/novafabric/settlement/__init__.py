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

"""Agentic-commerce settlement evidence (ADR-0163, experimental).

Record-only: NovaFabric records the provenance of money another system
moved. It never processes payments, holds or moves funds, provisions tokens,
or adjudicates a dispute.
"""

from novafabric.settlement.facet import (
    FACET_NAME,
    SCHEMA_VERSION,
    UNKNOWN_FINALITY,
    AuthorizedTerms,
    Discrepancy,
    DiscrepancyLaunderedError,
    FinalityRecord,
    FinalitySource,
    FinalityState,
    InvalidReferenceError,
    MandateReconciliation,
    Money,
    NonRepudiationBinding,
    ObservedSettlement,
    PaymentSecretRejectedError,
    Protocol,
    SettlementFacet,
    SigScheme,
    UnconfirmedFinalityError,
    attach_facet,
    build_facet,
    build_non_repudiation,
    digest_artifact,
    finality_state,
    is_final,
    reconcile,
    reject_payment_secrets,
    verify_non_repudiation,
    verify_ref_binding,
)

__all__ = [
    "FACET_NAME",
    "SCHEMA_VERSION",
    "UNKNOWN_FINALITY",
    "AuthorizedTerms",
    "Discrepancy",
    "DiscrepancyLaunderedError",
    "FinalityRecord",
    "FinalitySource",
    "FinalityState",
    "InvalidReferenceError",
    "MandateReconciliation",
    "Money",
    "NonRepudiationBinding",
    "ObservedSettlement",
    "PaymentSecretRejectedError",
    "Protocol",
    "SettlementFacet",
    "SigScheme",
    "UnconfirmedFinalityError",
    "attach_facet",
    "build_facet",
    "build_non_repudiation",
    "digest_artifact",
    "finality_state",
    "is_final",
    "reconcile",
    "reject_payment_secrets",
    "verify_non_repudiation",
    "verify_ref_binding",
]
