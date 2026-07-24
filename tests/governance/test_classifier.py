# tests/governance/test_classifier.py
"""Tests for the risk-tier classifier (ADR-0056).

Covers:
  - EU AI Act prohibited tier: social scoring, real-time biometric surveillance
  - Annex III category 1: biometrics / emotion recognition
  - Annex III category 4: employment (recruitment, work monitoring)
  - Annex III category 5: finance / credit scoring
  - Limited-risk detection: chatbot
  - Minimal-risk detection: spam filter
  - NIST AI RMF impact levels: low, medium, high, critical
  - OMB M-24-10 flag assignment
  - Vocabulary loading from default path
  - ClassificationResult structure / Pydantic model validation
  - CLI smoke: high_risk exit code 0 for finance / credit scoring
  - CLI smoke: prohibited tier → exit code 1
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app as nova_app
from novafabric.governance import AISystemRecord, ClassificationResult, RiskTierClassifier
from novafabric.governance.models import AnnotatedItem

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def classifier() -> RiskTierClassifier:
    return RiskTierClassifier()


def _record(**kwargs) -> AISystemRecord:  # type: ignore[no-untyped-def]
    defaults = {
        "name": "test-system",
        "description": "A test AI system",
        "use_case_domain": "general",
        "deployment_context": "testing",
    }
    defaults.update(kwargs)
    return AISystemRecord(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# EU AI Act — Prohibited tier
# ---------------------------------------------------------------------------

class TestProhibitedTier:
    def test_social_scoring_detected(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="SocialScore",
            use_case_domain="government",
            description="System that rates citizens via a social credit score",
            deployment_context="citizen social credit scoring in public authority",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "prohibited"
        item_ids = [i.item_id for i in result.eu_ai_act_annex_iii_items]
        assert "social_scoring" in item_ids

    def test_real_time_biometric_surveillance_detected(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="LiveFaceID",
            use_case_domain="law_enforcement",
            description="Real-time face recognition surveillance in public spaces",
            deployment_context="real-time biometric identification in public space",
            uses_biometrics=True,
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "prohibited"
        item_ids = [i.item_id for i in result.eu_ai_act_annex_iii_items]
        assert "real_time_biometric_surveillance" in item_ids

    def test_prohibited_citations_non_empty(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="SubBot",
            use_case_domain="general",
            description="Subliminal manipulation of users below threshold of consciousness",
            deployment_context="subliminal ad insertion",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "prohibited"
        assert len(result.citations) >= 1
        assert all("2024/1689" in c for c in result.citations)

    def test_prohibited_tier_has_role(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="SocialScore2",
            use_case_domain="government",
            description="social score system for public authority",
            deployment_context="citizen rating by public authority",
            operator_role="provider",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_role == "provider"


# ---------------------------------------------------------------------------
# EU AI Act — Annex III high_risk
# ---------------------------------------------------------------------------

class TestHighRiskAnnexIII:
    def test_category_1_biometric_categorisation(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="FaceGroup",
            use_case_domain="general",
            description="Categorises persons via face recognition and biometric features",
            deployment_context="biometric access control",
            uses_biometrics=True,
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "high_risk"
        item_ids = [i.item_id for i in result.eu_ai_act_annex_iii_items]
        assert "biometric_categorisation" in item_ids

    def test_category_1_emotion_recognition_education(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="EmotionScan",
            use_case_domain="education",
            description="Detects student stress and emotion during exams",
            deployment_context="emotion recognition in educational institution",
            uses_biometrics=True,
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "high_risk"
        item_ids = [i.item_id for i in result.eu_ai_act_annex_iii_items]
        assert "emotion_recognition" in item_ids

    def test_category_4_recruitment_screening(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="CVFilter",
            use_case_domain="employment",
            description="Automated CV screening and job applicant ranking for recruitment",
            deployment_context="recruitment screening and hiring decisions",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "high_risk"
        item_ids = [i.item_id for i in result.eu_ai_act_annex_iii_items]
        assert "recruitment_screening" in item_ids

    def test_category_4_work_allocation_monitoring(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="WorkMonitor",
            use_case_domain="employment",
            description="Monitors employee productivity and allocation for performance review",
            deployment_context="employee performance monitoring and task allocation",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "high_risk"
        item_ids = [i.item_id for i in result.eu_ai_act_annex_iii_items]
        assert "work_allocation_monitoring" in item_ids

    def test_category_5_credit_scoring(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="CreditBot",
            use_case_domain="finance",
            description="Assesses creditworthiness and credit score for loan decisions",
            deployment_context="credit scoring for personal loan underwriting",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "high_risk"
        item_ids = [i.item_id for i in result.eu_ai_act_annex_iii_items]
        assert "creditworthiness_assessment" in item_ids

    def test_category_5_health_insurance_risk(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="InsureRisk",
            use_case_domain="insurance",
            description="Actuarial risk assessment for health insurance and life insurance",
            deployment_context="life insurance risk assessment underwriting",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "high_risk"
        item_ids = [i.item_id for i in result.eu_ai_act_annex_iii_items]
        assert "life_health_insurance_risk" in item_ids

    def test_high_risk_citations_reference_annex_iii(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="CreditBot2",
            use_case_domain="finance",
            description="credit scoring",
            deployment_context="credit score assessment for banking",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "high_risk"
        assert any("Annex III" in c for c in result.citations)


# ---------------------------------------------------------------------------
# EU AI Act — Limited risk
# ---------------------------------------------------------------------------

class TestLimitedRisk:
    def test_chatbot_is_limited_risk(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="SupportBot",
            use_case_domain="retail",
            description="Customer service chatbot for order tracking",
            deployment_context="customer service virtual assistant",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "limited_risk"
        assert result.eu_ai_act_annex_iii_items == []

    def test_recommendation_engine_limited_risk(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="RecoEngine",
            use_case_domain="entertainment",
            description="Movie recommendation engine for streaming platform",
            deployment_context="content recommendation to viewers",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "limited_risk"


# ---------------------------------------------------------------------------
# EU AI Act — Minimal risk
# ---------------------------------------------------------------------------

class TestMinimalRisk:
    def test_spam_filter_is_minimal_risk(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="SpamFilter",
            use_case_domain="productivity",
            description="Email spam filter using ML-based content analysis",
            deployment_context="spam filter for corporate email",
        )
        result = classifier.classify(record)
        assert result.eu_ai_act_tier == "minimal_risk"
        assert result.eu_ai_act_annex_iii_items == []

    def test_general_purpose_no_sensitive_domain_minimal(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="GenAI",
            use_case_domain="general",
            description="A general-purpose text generation assistant",
            deployment_context="internal productivity tool",
            is_general_purpose=True,
        )
        result = classifier.classify(record)
        # general + is_general_purpose → limited or minimal depending on keywords
        assert result.eu_ai_act_tier in ("limited_risk", "minimal_risk")


# ---------------------------------------------------------------------------
# NIST AI RMF impact levels
# ---------------------------------------------------------------------------

class TestNistRmfImpact:
    def test_critical_impact_law_enforcement_with_rights(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="RiskPredict",
            use_case_domain="law_enforcement",
            description="Recidivism risk prediction system for parole decisions",
            deployment_context="criminal risk assessment",
            affects_fundamental_rights=True,
        )
        result = classifier.classify(record)
        assert result.nist_rmf_impact == "critical"

    def test_high_impact_finance_domain(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="LoanBot",
            use_case_domain="finance",
            description="Automated loan approval system",
            deployment_context="credit decision engine",
        )
        result = classifier.classify(record)
        assert result.nist_rmf_impact == "high"

    def test_high_impact_healthcare_domain(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="DiagnosAI",
            use_case_domain="healthcare",
            description="Assists clinicians with diagnostic suggestions",
            deployment_context="clinical decision support",
        )
        result = classifier.classify(record)
        assert result.nist_rmf_impact == "high"

    def test_high_impact_via_subject_count(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="AnonBot",
            use_case_domain="general",
            description="A general search tool used at massive scale",
            deployment_context="enterprise search",
            subject_count_estimate=50_000,
        )
        result = classifier.classify(record)
        assert result.nist_rmf_impact == "high"

    def test_low_impact_entertainment(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="GameReco",
            use_case_domain="entertainment",
            description="Game recommendation engine",
            deployment_context="game suggestion for streaming app",
            is_general_purpose=True,
        )
        result = classifier.classify(record)
        assert result.nist_rmf_impact == "low"


# ---------------------------------------------------------------------------
# OMB M-24-10 flags
# ---------------------------------------------------------------------------

class TestOmbFlags:
    def test_rights_impacting_flag_set_for_finance(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="CreditDecider",
            use_case_domain="finance",
            description="Credit decision tool for mortgage applications",
            deployment_context="creditworthiness assessment",
        )
        result = classifier.classify(record)
        assert "rights_impacting" in result.omb_flags

    def test_high_impact_without_oversight_flag(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="CriminalProfiler",
            use_case_domain="law_enforcement",
            description="Predictive policing and criminal profiling tool",
            deployment_context="crime prediction",
            affects_fundamental_rights=True,
        )
        result = classifier.classify(record)
        assert "high_impact_without_oversight" in result.omb_flags

    def test_no_omb_flags_for_minimal_system(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="SpellCheck",
            use_case_domain="productivity",
            description="Spell checker and grammar correction tool",
            deployment_context="word processing assistant",
        )
        result = classifier.classify(record)
        # Should have no OMB flags
        assert result.omb_flags == []


# ---------------------------------------------------------------------------
# ClassificationResult structure
# ---------------------------------------------------------------------------

class TestClassificationResultStructure:
    def test_result_is_pydantic_model(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="Foo",
            use_case_domain="general",
            description="generic AI",
            deployment_context="internal tool",
        )
        result = classifier.classify(record)
        assert isinstance(result, ClassificationResult)

    def test_annotated_items_have_required_fields(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record(
            name="CVFilter3",
            use_case_domain="employment",
            description="CV screening system for job recruitment",
            deployment_context="recruitment hiring filter",
        )
        result = classifier.classify(record)
        for item in result.eu_ai_act_annex_iii_items:
            assert isinstance(item, AnnotatedItem)
            assert item.item_id
            assert item.description
            assert item.citation

    def test_vocabulary_version_non_empty(self, classifier: RiskTierClassifier) -> None:
        record = _record()
        result = classifier.classify(record)
        assert result.vocabulary_version
        assert "eu-ai-act" in result.vocabulary_version
        assert "nist-rmf" in result.vocabulary_version
        assert "omb-m24-10" in result.vocabulary_version

    def test_classification_capsule_id_defaults_none(
        self, classifier: RiskTierClassifier
    ) -> None:
        record = _record()
        result = classifier.classify(record)
        assert result.classification_capsule_id is None

    def test_result_serialises_to_json(self, classifier: RiskTierClassifier) -> None:
        record = _record(
            name="CreditBot4",
            use_case_domain="finance",
            description="credit score",
            deployment_context="credit scoring system",
        )
        result = classifier.classify(record)
        payload = json.loads(result.model_dump_json())
        assert payload["eu_ai_act_tier"] in (
            "prohibited", "high_risk", "limited_risk", "minimal_risk"
        )
        assert payload["nist_rmf_impact"] in ("low", "medium", "high", "critical")


# ---------------------------------------------------------------------------
# Vocabulary loading
# ---------------------------------------------------------------------------

class TestVocabularyLoading:
    def test_default_vocabulary_loads_without_error(self) -> None:
        clf = RiskTierClassifier()
        assert clf._vocab_version  # type: ignore[attr-defined]

    def test_custom_vocabulary_dir_raises_on_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            RiskTierClassifier(vocabulary_dir=tmp_path)


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

class TestCLI:
    def test_classify_run_finance_credit_exits_0(self) -> None:
        """Finance + credit scoring → high_risk, NOT prohibited → exit 0."""
        result = runner.invoke(
            nova_app,
            [
                "classify", "run",
                "--name", "CreditScorer",
                "--domain", "finance",
                "--context", "credit scoring for loan decisions",
                "--description", "creditworthiness assessment tool for banking",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "HIGH_RISK" in result.output

    def test_classify_run_prohibited_exits_1(self) -> None:
        """Social scoring → prohibited → exit code 1."""
        result = runner.invoke(
            nova_app,
            [
                "classify", "run",
                "--name", "SocialCreditSystem",
                "--domain", "government",
                "--context", "citizen social credit scoring by public authority",
                "--description", "social score and citizen rating system",
            ],
        )
        assert result.exit_code == 1, result.output
        assert "PROHIBITED" in result.output

    def test_classify_run_json_output(self) -> None:
        """--json flag produces parseable JSON."""
        result = runner.invoke(
            nova_app,
            [
                "classify", "run",
                "--name", "RecoBot",
                "--domain", "retail",
                "--context", "customer service chatbot",
                "--description", "virtual assistant for order support",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "eu_ai_act_tier" in payload
        assert "nist_rmf_impact" in payload

    def test_classify_list_vocabularies(self) -> None:
        """list-vocabularies prints vocabulary table without error."""
        result = runner.invoke(nova_app, ["classify", "list-vocabularies"])
        assert result.exit_code == 0, result.output
        assert "eu-ai-act" in result.output

    def test_classify_run_missing_flags_exits_2(self) -> None:
        """Missing --name/--domain/--context → exit 2."""
        result = runner.invoke(
            nova_app,
            ["classify", "run", "--name", "Incomplete"],
        )
        assert result.exit_code == 2, result.output
