"""ADR-0095 slice C3 — template renderers (Annex IV / NIST RMF / markdown / json).

These tests pin the *honesty is structural* contract of
``render_safety_case``: a CONTESTED claim must visibly render its contest reason, an
UNSUPPORTED claim must never be laundered into "compliant", and a not-quantified
residual risk must render as such (never a fabricated number).
"""

from __future__ import annotations

import json

import pytest

from novafabric.compliance.export.safety_case import render_safety_case
from novafabric.safetycase.models import (
    SAFETY_CASE_SCHEMA_VERSION,
    ArgumentStrategy,
    ArtifactRef,
    BackingState,
    Claim,
    ClaimKind,
    Confidence,
    ConfidenceMethod,
    EvalContext,
    Evidence,
    EvidenceKind,
    InferenceType,
    ProducerInfo,
    ResidualRisk,
    ResidualRiskBasis,
    SafetyCase,
    Subject,
    SubjectKind,
)

# --- fixtures ----------------------------------------------------------------


def _producer() -> ProducerInfo:
    return ProducerInfo(name="novafabric", version="0.1.0")


def _subject() -> Subject:
    return Subject(
        kind=SubjectKind.RUN_CAPSULE,
        run_ids=["01HXAY7M5JZ8R7K4P9DPBYK2WX"],
        capsule_hash="sha256:" + "a" * 64,
    )


def _supported_evidence() -> Evidence:
    return Evidence(
        id="E-replay-1",
        evidence_kind=EvidenceKind.REPLAY_ATTESTATION,
        artifact_ref=ArtifactRef(
            kind="reperformance",
            path_in_bundle="attestations/reperformance.json",
            sha256="sha256:" + "b" * 64,
        ),
        attestation_ref="attestations/reperformance.json",
    )


def _eval_evidence() -> Evidence:
    return Evidence(
        id="E-eval-1",
        evidence_kind=EvidenceKind.JUDGE_AGGREGATE,
        artifact_ref=ArtifactRef(
            kind="eval_result",
            path_in_bundle="eval-results/harm.json",
            sha256="sha256:" + "c" * 64,
        ),
        eval_context=EvalContext(
            judges=["judge-a", "judge-b"],
            inter_judge_agreement=0.41,
            consensus_verdict="pass (low agreement)",
            sample_size=120,
            confidence_interval=(0.52, 0.79),
            process_evidence_ref="tool-calls.jsonl#unsafe",
        ),
    )


def _annex_iv_case() -> SafetyCase:
    """A two-element Annex IV-shaped case: one SUPPORTED, one CONTESTED leaf."""
    nodes = [
        Claim(
            id="C-root",
            statement="Agent meets EU AI Act Annex IV requirements.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.CONTESTED,
            children=["A-root", "C-elem-3", "C-elem-15"],
            contest_reason="Propagated from children: 1 sub-claim(s) CONTESTED.",
        ),
        ArgumentStrategy(
            id="A-root",
            strategy="Decompose into Annex IV elements.",
            inference_type=InferenceType.INDUCTIVE,
        ),
        _supported_evidence(),
        Claim(
            id="C-elem-3",
            statement="Annex IV element 3: monitoring, functioning and control.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.SUPPORTED,
            evidence_ids=["E-replay-1"],
        ),
        _eval_evidence(),
        Claim(
            id="C-elem-15",
            statement="Annex IV element 15: accuracy/robustness test reports.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.CONTESTED,
            evidence_ids=["E-eval-1"],
            confidence=Confidence(
                interval=(0.52, 0.79), method=ConfidenceMethod.WILSON
            ),
            contest_reason="Inter-judge agreement κ=0.41 (< 0.6).",
        ),
    ]
    return SafetyCase(
        schema_version=SAFETY_CASE_SCHEMA_VERSION,
        case_id="01HXAY7M5JZ8R7K4P9DPBYK2WX",
        created_at="2026-06-19T00:00:00+00:00",
        created_by=_producer(),
        template_id="eu-ai-act-annex-iv-v0",
        subject=_subject(),
        top_claim_id="C-root",
        nodes=nodes,
        residual_risk=ResidualRisk(
            value=None,
            basis=ResidualRiskBasis.NOT_QUANTIFIED,
            caveat="Top claim backing_state is CONTESTED.",
        ),
    ).with_case_hash()


def _unsupported_case() -> SafetyCase:
    """A case whose single element is UNSUPPORTED (an honest hole)."""
    nodes = [
        Claim(
            id="C-root",
            statement="Agent meets EU AI Act Annex IV requirements.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.UNSUPPORTED,
            children=["C-elem-9"],
        ),
        Claim(
            id="C-elem-9",
            statement="Annex IV element 9: post-market monitoring system.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.UNSUPPORTED,
        ),
    ]
    return SafetyCase(
        case_id="01HXAY7M5JZ8R7K4P9DPBYK2WY",
        created_at="2026-06-19T00:00:00+00:00",
        created_by=_producer(),
        template_id="eu-ai-act-annex-iv-v0",
        subject=_subject(),
        top_claim_id="C-root",
        nodes=nodes,
        residual_risk=ResidualRisk(basis=ResidualRiskBasis.NOT_QUANTIFIED),
    ).with_case_hash()


def _nist_case() -> SafetyCase:
    """A NIST-RMF-shaped case grouped by function id (GOVERN/MAP/MEASURE/MANAGE)."""
    nodes = [
        Claim(
            id="C-root",
            statement="Risks mapped, measured, managed, governed per NIST AI RMF.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.UNKNOWN,
            children=["C-GOVERN", "C-MAP", "C-MEASURE", "C-MANAGE"],
        ),
        Claim(
            id="C-GOVERN",
            statement="GOVERN: governance controls are in place.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.UNKNOWN,
        ),
        Claim(
            id="C-MAP",
            statement="MAP: context and risks are mapped.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.UNSUPPORTED,
        ),
        Claim(
            id="C-MEASURE",
            statement="MEASURE: risks are measured.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.SUPPORTED,
            evidence_ids=["E-replay-1"],
        ),
        _supported_evidence(),
        Claim(
            id="C-MANAGE",
            statement="MANAGE: risks are managed.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.SUPPORTED,
        ),
    ]
    return SafetyCase(
        case_id="01HXAY7M5JZ8R7K4P9DPBYK2WZ",
        created_at="2026-06-19T00:00:00+00:00",
        created_by=_producer(),
        template_id="nist-ai-rmf-v0",
        subject=_subject(),
        top_claim_id="C-root",
        nodes=nodes,
        residual_risk=ResidualRisk(basis=ResidualRiskBasis.NOT_QUANTIFIED),
    ).with_case_hash()


# --- annex-iv honesty contract ----------------------------------------------


class TestAnnexIV:
    def test_renders_header_and_elements(self) -> None:
        out = render_safety_case(_annex_iv_case(), "annex-iv")
        assert "EU AI Act" in out
        assert "Annex IV" in out
        # each claim renders as a numbered element
        assert "C-elem-3" in out
        assert "C-elem-15" in out

    def test_supported_element_shows_state_honestly(self) -> None:
        out = render_safety_case(_annex_iv_case(), "annex-iv")
        assert "SUPPORTED" in out

    def test_contested_element_renders_contest_reason(self) -> None:
        out = render_safety_case(_annex_iv_case(), "annex-iv")
        assert "CONTESTED" in out
        assert "κ=0.41" in out  # the actual contest reason text is surfaced

    def test_evidence_sha256_is_referenced(self) -> None:
        out = render_safety_case(_annex_iv_case(), "annex-iv")
        assert "sha256:" + "b" * 64 in out

    def test_unsupported_is_never_laundered_to_compliant(self) -> None:
        out = render_safety_case(_unsupported_case(), "annex-iv")
        assert "UNSUPPORTED" in out
        # honesty: the word "compliant" must not be asserted over an unsupported claim
        assert "compliant" not in out.lower()

    def test_residual_risk_not_quantified_renders_as_such(self) -> None:
        out = render_safety_case(_annex_iv_case(), "annex-iv")
        assert "not-quantified" in out
        # never a fabricated number
        assert "0.75%" not in out


class TestNistRmf:
    def test_grouped_by_function(self) -> None:
        out = render_safety_case(_nist_case(), "nist-rmf")
        for fn in ("GOVERN", "MAP", "MEASURE", "MANAGE"):
            assert fn in out

    def test_states_rendered_honestly(self) -> None:
        out = render_safety_case(_nist_case(), "nist-rmf")
        assert "UNSUPPORTED" in out
        assert "UNKNOWN" in out
        assert "SUPPORTED" in out

    def test_ungrouped_claims_still_appear(self) -> None:
        # a NIST case whose claim ids do not encode a function still renders fully
        case = _annex_iv_case()
        out = render_safety_case(case, "nist-rmf")
        assert "C-elem-3" in out


class TestGenericFallbacks:
    def test_markdown(self) -> None:
        out = render_safety_case(_annex_iv_case(), "markdown")
        assert "# Safety Case" in out
        assert "C-root" in out

    def test_json_roundtrips(self) -> None:
        out = render_safety_case(_annex_iv_case(), "json")
        data = json.loads(out)
        assert data["template_id"] == "eu-ai-act-annex-iv-v0"
        assert data["case_hash"].startswith("sha256:")

    def test_unknown_format_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown"):
            render_safety_case(_annex_iv_case(), "totally-bogus")  # type: ignore[arg-type]


class TestResidualRiskEdges:
    def _no_risk_case(self) -> SafetyCase:
        case = _annex_iv_case()
        return case.model_copy(update={"residual_risk": None}).with_case_hash()

    def _measured_risk_case(self) -> SafetyCase:
        case = _annex_iv_case()
        rr = ResidualRisk(
            value=0.012, basis=ResidualRiskBasis.MEASURED, caveat="measured on hold-out"
        )
        return case.model_copy(update={"residual_risk": rr}).with_case_hash()

    @pytest.mark.parametrize("fmt", ["annex-iv", "nist-rmf", "markdown"])
    def test_none_residual_risk_renders_not_recorded(self, fmt: str) -> None:
        out = render_safety_case(self._no_risk_case(), fmt)  # type: ignore[arg-type]
        assert "not recorded" in out

    @pytest.mark.parametrize("fmt", ["annex-iv", "nist-rmf", "markdown"])
    def test_measured_value_renders_with_measured_basis(self, fmt: str) -> None:
        out = render_safety_case(self._measured_risk_case(), fmt)  # type: ignore[arg-type]
        assert "0.012" in out
        assert "measured" in out


class TestDanglingEvidence:
    def test_dangling_evidence_id_is_flagged_not_dropped(self) -> None:
        # a claim referencing an evidence id that has no node renders a visible note
        nodes = [
            Claim(
                id="C-root",
                statement="root",
                claim_kind=ClaimKind.COMPLIANCE,
                backing_state=BackingState.SUPPORTED,
                children=["C-leaf"],
            ),
            Claim(
                id="C-leaf",
                statement="leaf with a dangling evidence ref",
                claim_kind=ClaimKind.COMPLIANCE,
                backing_state=BackingState.SUPPORTED,
                evidence_ids=["E-missing"],
            ),
        ]
        case = SafetyCase(
            case_id="01HXAY7M5JZ8R7K4P9DPBYK2WX",
            created_at="2026-06-19T00:00:00+00:00",
            created_by=_producer(),
            template_id="clymer-generic-v0",
            subject=_subject(),
            top_claim_id="C-root",
            nodes=nodes,
        ).with_case_hash()
        out = render_safety_case(case, "annex-iv")
        assert "dangling reference" in out
