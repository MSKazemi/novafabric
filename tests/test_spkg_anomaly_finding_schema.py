# tests/test_spkg_anomaly_finding_schema.py
"""Schema contract tests for the SPKG AnomalyFinding (ADR-0111, Phase P1, BQ-SPKG-01).

The invariant under test (ADR-0111 R2): every anomaly finding MUST carry an explanation
mapped to a MITRE ATT&CK technique and/or a D3FEND artifact — a bare score is not a valid
finding. These tests fix that contract before any detector is implemented.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = Path("schemas/spkg-anomaly-finding-v1.schema.json")
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def finding_schema() -> dict:  # type: ignore[type-arg]
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def valid_finding() -> dict:  # type: ignore[type-arg]
    return json.loads((FIXTURES_DIR / "spkg_anomaly_finding_valid.json").read_text())


def test_valid_fixture_passes(finding_schema: dict, valid_finding: dict) -> None:  # type: ignore[type-arg]
    jsonschema.validate(
        valid_finding, finding_schema, format_checker=jsonschema.FormatChecker()
    )


def test_invalid_fixture_rejected_missing_attack_mapping(finding_schema: dict) -> None:  # type: ignore[type-arg]
    """A finding with a rationale but no ATT&CK/D3FEND mapping violates R2."""
    data = json.loads((FIXTURES_DIR / "spkg_anomaly_finding_invalid.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            data, finding_schema, format_checker=jsonschema.FormatChecker()
        )


def test_d3fend_only_explanation_is_valid(finding_schema: dict, valid_finding: dict) -> None:  # type: ignore[type-arg]
    """anyOf allows a D3FEND artifact alone (no ATT&CK id required)."""
    data = copy.deepcopy(valid_finding)
    data["explanation"] = {
        "rationale": "Deviant process lineage vs learned baseline.",
        "d3fend_artifact": "d3f:ProcessLineage",
    }
    jsonschema.validate(data, finding_schema, format_checker=jsonschema.FormatChecker())


def test_score_out_of_range_rejected(finding_schema: dict, valid_finding: dict) -> None:  # type: ignore[type-arg]
    data = copy.deepcopy(valid_finding)
    data["score"] = 1.5
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, finding_schema, format_checker=jsonschema.FormatChecker())


def test_bad_attack_technique_id_rejected(finding_schema: dict, valid_finding: dict) -> None:  # type: ignore[type-arg]
    data = copy.deepcopy(valid_finding)
    data["explanation"] = {
        "rationale": "x",
        "attack_technique_id": "1059",  # missing leading 'T' + 4 digits
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, finding_schema, format_checker=jsonschema.FormatChecker())


def test_unknown_method_rejected(finding_schema: dict, valid_finding: dict) -> None:  # type: ignore[type-arg]
    data = copy.deepcopy(valid_finding)
    data["method"] = "handcrafted_signature"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, finding_schema, format_checker=jsonschema.FormatChecker())


def test_additional_properties_rejected(finding_schema: dict, valid_finding: dict) -> None:  # type: ignore[type-arg]
    data = copy.deepcopy(valid_finding)
    data["surprise_extra_field"] = "nope"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, finding_schema, format_checker=jsonschema.FormatChecker())
