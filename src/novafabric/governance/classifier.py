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
"""Risk-tier classifier for AI systems (ADR-0056).

Deterministic, rules-based classification against:
  - EU AI Act (Regulation EU 2024/1689) — prohibited / high-risk / limited / minimal
  - NIST AI RMF 600-1 — low / medium / high / critical impact
  - OMB M-24-10 — rights-impacting, safety-impacting, high-impact-without-oversight,
                   inventory-required flags
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import AISystemRecord, AnnotatedItem, ClassificationResult

_DEFAULT_VOCAB_DIR = Path(__file__).parent / "vocabularies"

_EU_AI_ACT_VOCAB = "eu-ai-act/2024.1.0.yaml"
_NIST_RMF_VOCAB = "nist-ai-rmf/1.0.0.yaml"
_OMB_VOCAB = "omb-m24-10/1.0.0.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Vocabulary file {path} did not parse as a mapping")
    return data


def _keyword_match(text_corpus: str, keywords: list[str]) -> bool:
    """Return True if any keyword appears (case-insensitive) in *text_corpus*."""
    corpus = text_corpus.lower()
    return any(kw.lower() in corpus for kw in keywords)


def _domain_match(record_domain: str, domains: list[str]) -> bool:
    return record_domain.lower() in [d.lower() for d in domains]


def _build_corpus(record: AISystemRecord) -> str:
    """Concatenate all free-text fields into a single searchable string."""
    return " ".join(
        [record.description, record.deployment_context, record.use_case_domain]
    )


def _triggers_match(
    triggers: dict[str, Any],
    record: AISystemRecord,
    corpus: str,
) -> bool:
    """Return True if ALL conditions in *triggers* are satisfied."""
    # --- keyword gate ---
    kw_list: list[str] = triggers.get("keywords", [])
    if kw_list and not _keyword_match(corpus, kw_list):
        return False

    # --- domain gate (any match is sufficient) ---
    domain_list: list[str] = triggers.get("domains", [])
    if domain_list and not _domain_match(record.use_case_domain, domain_list):
        return False

    # --- boolean flag gates ---
    if triggers.get("uses_biometrics") is True and not record.uses_biometrics:
        return False
    if triggers.get("affects_fundamental_rights") is True and not record.affects_fundamental_rights:
        return False

    return True


class RiskTierClassifier:
    """Classify an :class:`AISystemRecord` against EU AI Act, NIST AI RMF, and OMB M-24-10.

    Vocabularies are loaded from YAML files bundled with this package.
    A custom *vocabulary_dir* may be supplied for testing or overrides.
    """

    def __init__(self, vocabulary_dir: Path | None = None) -> None:
        vd = vocabulary_dir or _DEFAULT_VOCAB_DIR
        self._eu_vocab: dict[str, Any] = _load_yaml(vd / _EU_AI_ACT_VOCAB)
        self._nist_vocab: dict[str, Any] = _load_yaml(vd / _NIST_RMF_VOCAB)
        self._omb_vocab: dict[str, Any] = _load_yaml(vd / _OMB_VOCAB)
        self._vocab_version: str = (
            f"eu-ai-act:{self._eu_vocab['version']}/"
            f"nist-rmf:{self._nist_vocab['version']}/"
            f"omb-m24-10:{self._omb_vocab['version']}"
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def classify(self, record: AISystemRecord) -> ClassificationResult:
        """Run the 4-stage rules pipeline and return a :class:`ClassificationResult`."""
        corpus = _build_corpus(record)

        # Stage 1: Prohibited check
        prohibited_items = self._check_prohibited(record, corpus)

        # Stage 2: Annex III high-risk check
        annex_iii_items = self._check_annex_iii(record, corpus)

        # Stage 3: Determine EU AI Act tier
        if prohibited_items:
            eu_tier: str = "prohibited"
            matched_items = prohibited_items
        elif annex_iii_items:
            eu_tier = "high_risk"
            matched_items = annex_iii_items
        elif self._is_limited_risk(record, corpus):
            eu_tier = "limited_risk"
            matched_items = []
        else:
            eu_tier = "minimal_risk"
            matched_items = []

        # Stage 4: NIST RMF impact
        nist_impact = self._classify_nist_rmf(record, corpus)

        # Stage 5: OMB flags
        omb_flags = self._classify_omb_flags(record, nist_impact)

        # Build rationale + citations
        rationale = self._build_rationale(
            record=record,
            eu_tier=eu_tier,
            matched_items=matched_items,
            nist_impact=nist_impact,
            omb_flags=omb_flags,
        )
        citations = [item.citation for item in matched_items]
        citations = list(dict.fromkeys(citations))  # deduplicate, preserve order

        return ClassificationResult(
            eu_ai_act_tier=eu_tier,  # type: ignore[arg-type]
            eu_ai_act_annex_iii_items=matched_items,
            eu_ai_act_role=record.operator_role,
            nist_rmf_impact=nist_impact,  # type: ignore[arg-type]
            omb_flags=omb_flags,
            rationale=rationale,
            citations=citations,
            vocabulary_version=self._vocab_version,
        )

    # ------------------------------------------------------------------
    # Private — EU AI Act stages
    # ------------------------------------------------------------------

    def _check_prohibited(
        self, record: AISystemRecord, corpus: str
    ) -> list[AnnotatedItem]:
        matched: list[AnnotatedItem] = []
        for entry in self._eu_vocab.get("prohibited", []):
            triggers: dict[str, Any] = entry.get("triggers", {})
            if _triggers_match(triggers, record, corpus):
                matched.append(
                    AnnotatedItem(
                        item_id=entry["id"],
                        description=entry["description"],
                        citation=entry["citation"],
                    )
                )
        return matched

    def _check_annex_iii(
        self, record: AISystemRecord, corpus: str
    ) -> list[AnnotatedItem]:
        matched: list[AnnotatedItem] = []
        annex = self._eu_vocab.get("high_risk_annex_iii", {})
        for _category_name, entries in annex.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                triggers: dict[str, Any] = entry.get("triggers", {})
                if _triggers_match(triggers, record, corpus):
                    matched.append(
                        AnnotatedItem(
                            item_id=entry["id"],
                            description=entry["description"],
                            citation=entry["citation"],
                        )
                    )
        return matched

    def _is_limited_risk(self, record: AISystemRecord, corpus: str) -> bool:
        indicators: list[dict[str, Any]] = self._eu_vocab.get(
            "limited_risk_indicators", []
        )
        for indicator in indicators:
            kw_list: list[str] = indicator.get("keywords", [])
            domain_list: list[str] = indicator.get("domains", [])
            if kw_list and _keyword_match(corpus, kw_list):
                return True
            if domain_list and _domain_match(record.use_case_domain, domain_list):
                return True
        return False

    # ------------------------------------------------------------------
    # Private — NIST AI RMF impact
    # ------------------------------------------------------------------

    def _classify_nist_rmf(self, record: AISystemRecord, corpus: str) -> str:
        levels: dict[str, Any] = self._nist_vocab.get("impact_levels", {})

        # Check critical first (most specific)
        critical_cfg = levels.get("critical", {})
        critical_domains: list[str] = critical_cfg.get("domains", [])
        if record.affects_fundamental_rights and _domain_match(
            record.use_case_domain, critical_domains
        ):
            return "critical"

        # Check high
        high_cfg = levels.get("high", {})
        high_domains: list[str] = high_cfg.get("domains", [])
        subject_threshold: int | None = high_cfg.get("subject_count_threshold")
        if _domain_match(record.use_case_domain, high_domains):
            return "high"
        if (
            subject_threshold is not None
            and record.subject_count_estimate is not None
            and record.subject_count_estimate >= subject_threshold
        ):
            return "high"

        # Check medium
        medium_cfg = levels.get("medium", {})
        medium_domains: list[str] = medium_cfg.get("domains", [])
        if _domain_match(record.use_case_domain, medium_domains):
            return "medium"

        # General-purpose with no domain match → low
        if record.is_general_purpose:
            return "low"

        # Check low domains explicitly
        low_cfg = levels.get("low", {})
        low_domains: list[str] = low_cfg.get("domains", [])
        if _domain_match(record.use_case_domain, low_domains):
            return "low"

        # Default to medium (unknown domain, some plausible impact assumed)
        return "medium"

    # ------------------------------------------------------------------
    # Private — OMB M-24-10 flags
    # ------------------------------------------------------------------

    def _classify_omb_flags(
        self, record: AISystemRecord, nist_impact: str
    ) -> list[str]:
        flags_vocab: dict[str, Any] = self._omb_vocab.get("flags", {})
        result: list[str] = []

        for flag_name, flag_cfg in flags_vocab.items():
            triggers: dict[str, Any] = flag_cfg.get("triggers", {})
            if self._omb_trigger_match(triggers, record, nist_impact):
                result.append(flag_name)

        return result

    def _omb_trigger_match(
        self,
        triggers: dict[str, Any],
        record: AISystemRecord,
        nist_impact: str,
    ) -> bool:
        """OMB triggers use OR logic across conditions (any match fires the flag)."""
        if triggers.get("affects_fundamental_rights") and record.affects_fundamental_rights:
            return True

        domain_list: list[str] = triggers.get("domains", [])
        if domain_list and _domain_match(record.use_case_domain, domain_list):
            return True

        impact_list: list[str] = triggers.get("nist_rmf_impact", [])
        if impact_list and nist_impact in impact_list:
            return True

        return False

    # ------------------------------------------------------------------
    # Private — rationale builder
    # ------------------------------------------------------------------

    def _build_rationale(
        self,
        record: AISystemRecord,
        eu_tier: str,
        matched_items: list[AnnotatedItem],
        nist_impact: str,
        omb_flags: list[str],
    ) -> str:
        lines: list[str] = [
            f"AI system '{record.name}' classified as EU AI Act tier: {eu_tier.upper()}.",
            f"Domain: {record.use_case_domain} | Context: {record.deployment_context}.",
        ]

        if matched_items:
            lines.append("Matched regulatory items:")
            for item in matched_items:
                lines.append(f"  - [{item.item_id}] {item.description} ({item.citation})")

        lines.append(f"NIST AI RMF impact level: {nist_impact.upper()}.")

        if omb_flags:
            lines.append(f"OMB M-24-10 flags: {', '.join(omb_flags)}.")
        else:
            lines.append("No OMB M-24-10 flags triggered.")

        lines.append(f"Vocabularies used: {self._vocab_version}.")
        return "\n".join(lines)
