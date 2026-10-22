"""L18: tool-permission evidence is not captured by this version — say so, don't fail it.

``tool-permission-events.jsonl`` is read by the audit engine (``tool_permissions``,
``policy_events``) and the NIST AI RMF reporter (MP-2.1), but nothing in NovaFabric
writes it: ``CapsuleWriter.append_tool_permission_event`` has no caller. Before this
fix every capsule therefore reported those controls as *missing* / non-compliant —
a failed control the operator could not fix. When no scanned capsule carries the
file, the controls must report "not captured by this version" instead; when a
capsule does carry it (an SDK or third-party writer), it is evaluated as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from novafabric.capture.capsule import CapsuleWriter
from novafabric.compliance.audit import AuditEngine, load_profile
from novafabric.compliance.capture_coverage import (
    NOT_CAPTURED_EVIDENCE_TYPES,
    TOOL_PERMISSION_EVENTS_FILE,
)
from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

SRC = Path(__file__).resolve().parents[2] / "src" / "novafabric"


def _capture_like_capsule(root: Path, name: str = "run-001") -> Path:
    """A capsule carrying what a real ``nova capture`` writes — no permission stream."""
    capsule = root / "capsules" / name
    capsule.mkdir(parents=True)
    (capsule / "model-calls.jsonl").write_text(json.dumps({"model": "m"}) + "\n")
    (capsule / "lineage.jsonl").write_text(json.dumps({"edge_type": "contains"}) + "\n")
    (capsule / "capsule.yaml").write_text(yaml.safe_dump({"run_id": name, "status": "success"}))
    return capsule


def test_no_producer_writes_the_permission_stream() -> None:
    """The premise of this file. If a producer lands, delete the not-captured mapping."""
    callers = [
        p for p in SRC.rglob("*.py")
        if "append_tool_permission_event(" in p.read_text(encoding="utf-8")
        and p != SRC / "capture" / "capsule.py"
    ]
    assert not callers, f"a producer now exists — retire NOT_CAPTURED_EVIDENCE_TYPES: {callers}"
    writer_src = (SRC / "capture" / "capsule.py").read_text(encoding="utf-8")
    assert f'"{TOOL_PERMISSION_EVENTS_FILE}"' in writer_src
    assert hasattr(CapsuleWriter, "append_tool_permission_event")


def test_soc2_access_control_is_not_reported_missing(tmp_path: Path) -> None:
    _capture_like_capsule(tmp_path)
    report = AuditEngine(data_dir=tmp_path, profile=load_profile("soc2-type2")).scan()
    cc61 = next(c for c in report.coverages if c.control_id.startswith("CC6.1"))
    assert cc61.status == "not_applicable"
    assert cc61.evidence_missing == []
    assert cc61.evidence_not_captured, "the reason must be visible, not silent"
    assert all("not captured by this version" in n for n in cc61.evidence_not_captured)
    assert report.missing_controls < len(report.coverages)
    assert report.not_captured_controls >= 1


def test_not_captured_controls_do_not_drag_the_overall_score(tmp_path: Path) -> None:
    _capture_like_capsule(tmp_path)
    profile = load_profile("soc2-type2")
    report = AuditEngine(data_dir=tmp_path, profile=profile).scan()
    scored = [
        (cov, ctrl) for cov, ctrl in zip(report.coverages, profile.controls, strict=True)
        if cov.status != "not_applicable"
    ]
    weight = sum(ctrl.weight for _, ctrl in scored)
    expected = sum(cov.coverage_score * ctrl.weight for cov, ctrl in scored) / weight
    assert report.overall_score == round(expected, 4)


def test_mixed_control_is_partial_never_covered(tmp_path: Path) -> None:
    # ISO42-8.1 needs model_calls (captured, present) and tool_permissions (not captured):
    # the captured half is satisfied, but the control cannot claim full coverage.
    _capture_like_capsule(tmp_path)
    report = AuditEngine(data_dir=tmp_path, profile=load_profile("iso42001")).scan()
    ctrl = next(c for c in report.coverages if c.control_id == "ISO42-8.1-OPERATIONAL-PLANNING")
    assert ctrl.status == "partial"
    assert ctrl.coverage_score == 1.0
    assert ctrl.evidence_missing == []
    assert ctrl.evidence_not_captured


def test_a_capsule_that_carries_the_stream_is_still_evaluated(tmp_path: Path) -> None:
    capsule = _capture_like_capsule(tmp_path)
    (capsule / TOOL_PERMISSION_EVENTS_FILE).write_text(json.dumps({"decision": "allow"}) + "\n")
    report = AuditEngine(data_dir=tmp_path, profile=load_profile("soc2-type2")).scan()
    cc61 = next(c for c in report.coverages if c.control_id.startswith("CC6.1"))
    # tool_permissions satisfied; policy_events (needs a deny) genuinely missing now.
    assert cc61.status in {"covered", "partial"}
    assert cc61.evidence_not_captured == []
    assert report.not_captured_controls == 0


def test_not_captured_types_are_real_evidence_types() -> None:
    from novafabric.compliance.audit.engine import _EVIDENCE_CHECKERS

    assert set(NOT_CAPTURED_EVIDENCE_TYPES) <= set(_EVIDENCE_CHECKERS)


def test_nist_mp_2_1_is_not_captured_not_non_compliant(tmp_path: Path) -> None:
    capsule = _capture_like_capsule(tmp_path)
    report = NISTAIRMFReporter().build_report(capsule)
    mp21 = next(m for m in report.metrics if m.metric_id == "MP-2.1")
    assert mp21.compliant is None
    assert mp21.value is None
    assert mp21.measurement_method == "not_captured"
    assert "not captured by this version" in mp21.notes
    assert TOOL_PERMISSION_EVENTS_FILE not in report.missing_evidence
    assert TOOL_PERMISSION_EVENTS_FILE in report.not_captured_evidence


def test_nist_mp_2_1_still_scores_a_present_stream(tmp_path: Path) -> None:
    capsule = _capture_like_capsule(tmp_path)
    (capsule / TOOL_PERMISSION_EVENTS_FILE).write_text("{}\n")
    mp21 = next(
        m for m in NISTAIRMFReporter().build_report(capsule).metrics if m.metric_id == "MP-2.1"
    )
    assert mp21.compliant is True
    assert mp21.value == 1.0


def test_a_symlinked_stream_does_not_count_as_captured(tmp_path: Path) -> None:
    """Presence is checked without following links (``_capsule_fs``), like every reader."""
    from novafabric.compliance.capture_coverage import evidence_is_uncaptured

    capsule = _capture_like_capsule(tmp_path)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("{}\n")
    (capsule / TOOL_PERMISSION_EVENTS_FILE).symlink_to(target)
    assert evidence_is_uncaptured("tool_permissions", [capsule]) is True
