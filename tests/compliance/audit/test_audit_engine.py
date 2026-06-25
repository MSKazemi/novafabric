# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for the compliance audit control-mapping engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.compliance.audit import (
    AuditEngine,
    AuditReport,
    ControlProfile,
    list_profiles,
    load_profile,
)
from novafabric.compliance.audit.loader import BUILT_IN_PROFILES

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_test_capsule(tmp_path: Path) -> Path:
    """Create a minimal synthetic capsule directory for testing.

    Returns:
        *tmp_path* (the data root, containing ``capsules/test-run-001/``).
    """
    capsule_dir = tmp_path / "capsules" / "test-run-001"
    capsule_dir.mkdir(parents=True)
    (capsule_dir / "model-calls.jsonl").write_text(
        json.dumps({"model": "gpt-4", "run_id": "test-run-001"}) + "\n",
        encoding="utf-8",
    )
    (capsule_dir / "tool-permission-events.jsonl").write_text(
        json.dumps({"decision": "allow", "tool_name": "search"}) + "\n",
        encoding="utf-8",
    )
    (capsule_dir / "lineage.jsonl").write_text(
        json.dumps({"edge_type": "contains", "from": "parent", "to": "child"}) + "\n",
        encoding="utf-8",
    )
    return tmp_path


def make_full_capsule(tmp_path: Path) -> Path:
    """Create a capsule with ALL evidence types present."""
    capsule_dir = tmp_path / "capsules" / "full-run-001"
    capsule_dir.mkdir(parents=True)
    (capsule_dir / "model-calls.jsonl").write_text(
        json.dumps({"model": "gpt-4"}) + "\n",
        encoding="utf-8",
    )
    (capsule_dir / "tool-permission-events.jsonl").write_text(
        json.dumps({"decision": "deny", "tool_name": "dangerous"}) + "\n",
        encoding="utf-8",
    )
    (capsule_dir / "lineage.jsonl").write_text(
        json.dumps({"edge_type": "contains"}) + "\n",
        encoding="utf-8",
    )
    (capsule_dir / "seal-bundle.json").write_text(
        json.dumps({"signature": "abc123"}),
        encoding="utf-8",
    )
    pii_dir = capsule_dir / "compliance" / "pii"
    pii_dir.mkdir(parents=True)
    (pii_dir / "manifest.json").write_text(
        json.dumps({"scanned": True}),
        encoding="utf-8",
    )
    return tmp_path


def make_empty_data_dir(tmp_path: Path) -> Path:
    """Return a data root with no capsules directory."""
    return tmp_path


# ---------------------------------------------------------------------------
# Profile loading tests (one per built-in profile)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_id", BUILT_IN_PROFILES)
def test_load_builtin_profile(profile_id: str) -> None:
    """All built-in profiles load and parse as valid ControlProfile objects."""
    profile = load_profile(profile_id)
    assert isinstance(profile, ControlProfile)
    assert profile.id
    assert profile.name
    assert profile.framework
    assert profile.version
    assert len(profile.controls) > 0
    for ctrl in profile.controls:
        assert ctrl.id
        assert ctrl.title
        assert ctrl.description
        assert isinstance(ctrl.evidence_requirements, list)
        for req in ctrl.evidence_requirements:
            assert req.evidence_type
            assert req.description


def test_list_profiles_returns_all_builtins() -> None:
    """list_profiles() returns exactly the expected built-in profile IDs."""
    profiles = list_profiles()
    assert set(profiles) == set(BUILT_IN_PROFILES)
    assert "nist-ai-rmf" in profiles
    assert "gdpr" in profiles
    assert "eu-ai-act-high-risk" in profiles
    assert "soc2-type2" in profiles
    assert "iso42001" in profiles
    assert "scientific-reproducibility" in profiles


def test_load_unknown_profile_raises() -> None:
    """Loading an unknown profile ID raises ValueError."""
    with pytest.raises(ValueError, match="Unknown profile"):
        load_profile("no-such-profile-xyz")


# ---------------------------------------------------------------------------
# AuditEngine scan tests
# ---------------------------------------------------------------------------


def test_scan_empty_data_dir(tmp_path: Path) -> None:
    """Scanning a data dir with no capsules/ directory returns zero capsules."""
    profile = load_profile("nist-ai-rmf")
    engine = AuditEngine(data_dir=tmp_path, profile=profile)
    report = engine.scan()
    assert isinstance(report, AuditReport)
    assert report.capsule_count == 0
    assert report.total_controls == len(profile.controls)
    assert report.missing_controls == len(profile.controls)
    assert report.overall_score == 0.0


def test_scan_with_capsule_finds_model_calls(tmp_path: Path) -> None:
    """Capsule with model-calls.jsonl satisfies model_calls evidence requirements."""
    data_dir = make_test_capsule(tmp_path)
    profile = load_profile("nist-ai-rmf")
    engine = AuditEngine(data_dir=data_dir, profile=profile)
    report = engine.scan()
    assert report.capsule_count == 1
    # MAP-1.1 requires model_calls — should be covered
    map11 = next(c for c in report.coverages if c.control_id == "MAP-1.1")
    assert map11.status == "covered"
    assert map11.coverage_score == 1.0
    assert "test-run-001" in map11.capsule_ids


def test_scan_coverage_score_between_zero_and_one(tmp_path: Path) -> None:
    """Overall score is always in [0.0, 1.0]."""
    data_dir = make_test_capsule(tmp_path)
    profile = load_profile("nist-ai-rmf")
    engine = AuditEngine(data_dir=data_dir, profile=profile)
    report = engine.scan()
    assert 0.0 <= report.overall_score <= 1.0


def test_scan_full_capsule_has_higher_score_than_partial(tmp_path: Path) -> None:
    """A capsule with all evidence types scores higher than one with only model_calls."""
    partial_dir = make_test_capsule(tmp_path / "partial")
    full_dir = make_full_capsule(tmp_path / "full")

    profile = load_profile("nist-ai-rmf")

    engine_partial = AuditEngine(data_dir=partial_dir, profile=profile)
    report_partial = engine_partial.scan()

    engine_full = AuditEngine(data_dir=full_dir, profile=profile)
    report_full = engine_full.scan()

    assert report_full.overall_score >= report_partial.overall_score


def test_control_with_all_evidence_is_covered(tmp_path: Path) -> None:
    """A control whose required evidence is present in a capsule has status='covered'."""
    data_dir = make_full_capsule(tmp_path)
    profile = load_profile("gdpr")
    engine = AuditEngine(data_dir=data_dir, profile=profile)
    report = engine.scan()

    # GDPR-ART17-ERASURE requires pii_scan — which is present in full_capsule
    art17 = next(c for c in report.coverages if c.control_id == "GDPR-ART17-ERASURE")
    assert art17.status == "covered"
    assert art17.coverage_score == 1.0


def test_control_without_evidence_is_missing(tmp_path: Path) -> None:
    """A control whose required evidence is absent has status='missing'."""
    # Only model-calls.jsonl present — seal_signature is missing
    data_dir = make_test_capsule(tmp_path)
    profile = load_profile("gdpr")
    engine = AuditEngine(data_dir=data_dir, profile=profile)
    report = engine.scan()

    # GDPR-ART32-SECURITY requires seal_signature — absent in test capsule
    art32 = next(c for c in report.coverages if c.control_id == "GDPR-ART32-SECURITY")
    assert art32.status == "missing"
    assert art32.coverage_score == 0.0


def test_partial_coverage_status(tmp_path: Path) -> None:
    """A control with some required evidence missing is partial."""
    # iso42001 CC8.1 requires model_calls + tool_permissions + optional lineage
    # Make a capsule with only model_calls (not tool_permissions)
    capsule_dir = tmp_path / "capsules" / "partial-run-001"
    capsule_dir.mkdir(parents=True)
    (capsule_dir / "model-calls.jsonl").write_text(
        json.dumps({"model": "gpt-4"}) + "\n",
        encoding="utf-8",
    )

    profile = load_profile("iso42001")
    engine = AuditEngine(data_dir=tmp_path, profile=profile)
    report = engine.scan()

    # ISO42-8.1 requires model_calls AND tool_permissions
    op_ctrl = next(c for c in report.coverages if c.control_id == "ISO42-8.1-OPERATIONAL-PLANNING")
    # Has model_calls but not tool_permissions → partial
    assert op_ctrl.status == "partial"


def test_seal_verified_flag_when_seal_present(tmp_path: Path) -> None:
    """evidence_seal_verified is True when any capsule has seal-bundle.json."""
    data_dir = make_full_capsule(tmp_path)
    profile = load_profile("nist-ai-rmf")
    engine = AuditEngine(data_dir=data_dir, profile=profile)
    report = engine.scan()
    assert report.evidence_seal_verified is True


def test_seal_verified_flag_when_no_seal(tmp_path: Path) -> None:
    """evidence_seal_verified is False when no capsule has seal-bundle.json."""
    data_dir = make_test_capsule(tmp_path)
    profile = load_profile("nist-ai-rmf")
    engine = AuditEngine(data_dir=data_dir, profile=profile)
    report = engine.scan()
    assert report.evidence_seal_verified is False


# ---------------------------------------------------------------------------
# AuditReport model tests
# ---------------------------------------------------------------------------


def test_audit_report_json_ld_serialisation(tmp_path: Path) -> None:
    """AuditReport serialises with @context and @type JSON-LD fields."""
    data_dir = make_test_capsule(tmp_path)
    profile = load_profile("nist-ai-rmf")
    engine = AuditEngine(data_dir=data_dir, profile=profile)
    report = engine.scan()

    payload = json.loads(report.model_dump_json(by_alias=True))
    assert payload["@context"] == "https://schema.org/"
    assert payload["@type"] == "AuditReport"


def test_coverage_capsule_ids_are_non_empty_when_evidence_found(tmp_path: Path) -> None:
    """capsule_ids lists the capsule that contributed evidence."""
    data_dir = make_test_capsule(tmp_path)
    profile = load_profile("nist-ai-rmf")
    engine = AuditEngine(data_dir=data_dir, profile=profile)
    report = engine.scan()

    # MAP-1.1 covered → must have a capsule ID
    map11 = next(c for c in report.coverages if c.control_id == "MAP-1.1")
    assert len(map11.capsule_ids) > 0


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------


def test_audit_map_json_output(tmp_path: Path) -> None:
    """nova audit map --json emits valid JSON matching AuditReport schema."""
    data_dir = make_test_capsule(tmp_path)
    result = runner.invoke(
        app,
        [
            "audit", "map",
            "--data-dir", str(data_dir),
            "--profile", "nist-ai-rmf",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "@type" in payload
    assert payload["@type"] == "AuditReport"
    assert "overall_score" in payload
    assert "coverages" in payload


def test_audit_map_text_output(tmp_path: Path) -> None:
    """nova audit map (default text) exits 0 and prints a table."""
    data_dir = make_test_capsule(tmp_path)
    result = runner.invoke(
        app,
        [
            "audit", "map",
            "--data-dir", str(data_dir),
            "--profile", "gdpr",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Audit Report" in result.output


def test_audit_report_json_ld_to_file(tmp_path: Path) -> None:
    """nova audit report --output writes a valid JSON-LD file."""
    data_dir = make_test_capsule(tmp_path)
    out_file = tmp_path / "audit-report.json"
    result = runner.invoke(
        app,
        [
            "audit", "report",
            "--data-dir", str(data_dir),
            "--profile", "nist-ai-rmf",
            "--output", str(out_file),
            "--format", "json-ld",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_file.exists()
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["@type"] == "AuditReport"


def test_audit_report_text_format(tmp_path: Path) -> None:
    """nova audit report --format text emits plain text."""
    data_dir = make_test_capsule(tmp_path)
    result = runner.invoke(
        app,
        [
            "audit", "report",
            "--data-dir", str(data_dir),
            "--profile", "nist-ai-rmf",
            "--format", "text",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Audit Report:" in result.output
    assert "Overall score:" in result.output


def test_audit_verify_valid_report(tmp_path: Path) -> None:
    """nova audit verify accepts a well-formed audit report."""
    data_dir = make_test_capsule(tmp_path)
    profile = load_profile("nist-ai-rmf")
    engine = AuditEngine(data_dir=data_dir, profile=profile)
    report = engine.scan()

    report_file = tmp_path / "report.json"
    report_file.write_text(
        report.model_dump_json(by_alias=True, exclude_none=True),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["audit", "verify", str(report_file)])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output.lower()


def test_audit_verify_invalid_file(tmp_path: Path) -> None:
    """nova audit verify exits 1 for a malformed JSON file."""
    bad_file = tmp_path / "bad-report.json"
    bad_file.write_text("{not valid json", encoding="utf-8")
    result = runner.invoke(app, ["audit", "verify", str(bad_file)])
    assert result.exit_code == 1


def test_audit_coverage_passes_when_above_threshold(tmp_path: Path) -> None:
    """nova audit coverage exits 0 when score meets the threshold."""
    data_dir = make_full_capsule(tmp_path)
    # Use threshold=0.0 so that any coverage passes
    result = runner.invoke(
        app,
        [
            "audit", "coverage",
            "--data-dir", str(data_dir),
            "--profile", "nist-ai-rmf",
            "--threshold", "0.0",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_audit_coverage_fails_when_below_threshold(tmp_path: Path) -> None:
    """nova audit coverage exits 1 when score is below the threshold."""
    # Empty data dir → score = 0.0
    result = runner.invoke(
        app,
        [
            "audit", "coverage",
            "--data-dir", str(tmp_path),
            "--profile", "nist-ai-rmf",
            "--threshold", "0.5",
        ],
    )
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_audit_bundle_creates_zip(tmp_path: Path) -> None:
    """nova audit bundle creates a ZIP containing the audit report."""
    import zipfile

    data_dir = make_test_capsule(tmp_path)
    out_zip = tmp_path / "bundle.zip"
    result = runner.invoke(
        app,
        [
            "audit", "bundle",
            "--data-dir", str(data_dir),
            "--profile", "nist-ai-rmf",
            "--output", str(out_zip),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_zip.exists()
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
    assert "audit-report.json" in names
