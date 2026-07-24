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

"""Cost accounting and attribution (ADR-0132/0133/0146, experimental).

Record-only: NovaFabric attributes cost a run already incurred. It never
enforces a budget, throttles an agent, blocks a workload, or optimizes spend
— cost optimization is an explicit product non-goal (ADR-0146 I-4).

Only the ADR-0146 per-agent attribution surface is re-exported here; the
older run-total modules (``interceptor``, ``pricing_catalog``,
``usage_types``, …) keep their own import paths, so this package's ``__all__``
stays a reviewable statement of what the accountability layer exposes.
"""

from novafabric.cost.attribution import (
    FACET_NAME,
    SCHEMA_VERSION,
    AgentCost,
    Basis,
    ConservationCheck,
    ConservationError,
    CostAttributionFacet,
    CurrencyMismatchError,
    Money,
    RunTotal,
    UnapportionableError,
    apportion,
    attach_facet,
    build_facet,
    verify_conservation,
)

__all__ = [
    "FACET_NAME",
    "SCHEMA_VERSION",
    "AgentCost",
    "Basis",
    "ConservationCheck",
    "ConservationError",
    "CostAttributionFacet",
    "CurrencyMismatchError",
    "Money",
    "RunTotal",
    "UnapportionableError",
    "apportion",
    "attach_facet",
    "build_facet",
    "verify_conservation",
]
