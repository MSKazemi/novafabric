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
"""Pydantic models for the governance risk-tier classifier (ADR-0056)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AISystemRecord(BaseModel):
    """Description of an AI system submitted for risk-tier classification."""

    name: str
    description: str
    use_case_domain: str  # e.g. "healthcare", "finance", "education", "employment",
    #                          "law_enforcement", "critical_infrastructure", "general"
    deployment_context: str  # e.g. "medical_diagnosis", "credit_scoring", "recruitment"
    uses_biometrics: bool = False
    affects_fundamental_rights: bool = False
    is_general_purpose: bool = False
    operator_role: Literal["provider", "deployer"] = "deployer"
    subject_count_estimate: int | None = Field(
        default=None,
        description="Estimated number of persons directly affected by this AI system.",
    )


class AnnotatedItem(BaseModel):
    """A matched regulatory item with its citation."""

    item_id: str  # e.g. "annex_iii_5b"
    description: str
    citation: str  # e.g. "Regulation (EU) 2024/1689 Annex III item 5(b)"


class ClassificationResult(BaseModel):
    """Output of a single :class:`RiskTierClassifier` run."""

    eu_ai_act_tier: Literal["prohibited", "high_risk", "limited_risk", "minimal_risk"]
    eu_ai_act_annex_iii_items: list[AnnotatedItem]
    eu_ai_act_role: Literal["provider", "deployer"]
    nist_rmf_impact: Literal["low", "medium", "high", "critical"]
    omb_flags: list[str]
    rationale: str
    citations: list[str]
    vocabulary_version: str
    classification_capsule_id: str | None = None
