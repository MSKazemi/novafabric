"""Tests for cross_site_edge.schema.json (C-1)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "schemas"
    / "cross_site_edge.schema.json"
)

@pytest.fixture(scope="module")
def cross_site_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


# Crockford Base32 alphabet: 0-9 A-H J K M N P-T V-Z (no I L O U)
# All ULIDs below are exactly 26 characters using only valid characters.
_U1 = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_U2 = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
_U3 = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
_U4 = "01ARZ3NDEKTSV4RRFFQ69G5FAY"
_U5 = "01ARZ3NDEKTSV4RRFFQ69G5FA0"
_U6 = "01ARZ3NDEKTSV4RRFFQ69G5FA1"
_U7 = "01ARZ3NDEKTSV4RRFFQ69G5FA2"
_U8 = "01ARZ3NDEKTSV4RRFFQ69G5FA3"
_U9 = "01ARZ3NDEKTSV4RRFFQ69G5FA4"


# ---------------------------------------------------------------------------
# A valid local (same-site) edge — signature_ref present but may be empty
# when source_site_id == target_site_id (schema does not mandate non-empty
# for same-site edges; signature_ref field is still required by "required").
# ---------------------------------------------------------------------------

VALID_LOCAL_EDGE = {
    "edge_id": _U1,
    "source_run_id": _U2,
    "source_site_id": "site-alpha",
    "target_run_id": _U3,
    "target_site_id": "site-alpha",
    "edge_type": "contains",
    "signature_ref": "local-only",
}

VALID_CROSS_SITE_EDGE = {
    "edge_id": _U4,
    "source_run_id": _U5,
    "source_site_id": "site-alpha",
    "target_run_id": _U6,
    "target_site_id": "site-beta",
    "edge_type": "delegated_to",
    "signature_ref": "ns1:eyJhbGciOiJFUzI1NiJ9.dGVzdA.c2ln",
}

# Missing signature_ref altogether — invalid (required field absent).
MISSING_SIGNATURE_REF = {
    "edge_id": _U7,
    "source_run_id": _U8,
    "source_site_id": "site-alpha",
    "target_run_id": _U9,
    "target_site_id": "site-beta",
    "edge_type": "spawned",
    # signature_ref is intentionally absent
}

# Invalid edge_type.
INVALID_EDGE_TYPE = {
    "edge_id": _U1,
    "source_run_id": _U2,
    "source_site_id": "site-alpha",
    "target_run_id": _U3,
    "target_site_id": "site-beta",
    "edge_type": "unknown_type",
    "signature_ref": "ns1:token",
}

# source_site_id is an empty string — violates minLength: 1.
EMPTY_SOURCE_SITE = {
    "edge_id": _U4,
    "source_run_id": _U5,
    "source_site_id": "",
    "target_run_id": _U6,
    "target_site_id": "site-beta",
    "edge_type": "replayed_from",
    "signature_ref": "ns1:token",
}


def test_schema_file_exists() -> None:
    assert SCHEMA_PATH.exists(), f"Schema not found at {SCHEMA_PATH}"


def test_schema_is_valid_json(cross_site_schema: dict) -> None:
    assert isinstance(cross_site_schema, dict)
    assert cross_site_schema.get("$schema") == "http://json-schema.org/draft-07/schema#"


def test_required_fields_present(cross_site_schema: dict) -> None:
    required = cross_site_schema.get("required", [])
    for field in [
        "edge_id",
        "source_run_id",
        "source_site_id",
        "target_run_id",
        "target_site_id",
        "edge_type",
        "signature_ref",
    ]:
        assert field in required, f"Field '{field}' missing from required"


def test_valid_local_edge(cross_site_schema: dict) -> None:
    jsonschema.validate(VALID_LOCAL_EDGE, cross_site_schema)


def test_valid_cross_site_edge(cross_site_schema: dict) -> None:
    jsonschema.validate(VALID_CROSS_SITE_EDGE, cross_site_schema)


def test_invalid_missing_signature_ref(cross_site_schema: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(MISSING_SIGNATURE_REF, cross_site_schema)


def test_invalid_edge_type(cross_site_schema: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(INVALID_EDGE_TYPE, cross_site_schema)


def test_invalid_empty_source_site_id(cross_site_schema: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(EMPTY_SOURCE_SITE, cross_site_schema)
