"""Golden-fixture tests for the graduated MediaPart schema (ADR-0125).

Every fixture under tests/fixtures/multimodal-capture/ behaves as its filename
asserts, against BOTH copies of media-part.schema.json (root schemas/ and the
packaged src/novafabric/schemas/) — the two copies must also stay in sync on
the validating body.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "multimodal-capture"
SCHEMA_PATHS = (
    REPO_ROOT / "schemas" / "media-part.schema.json",
    REPO_ROOT / "src" / "novafabric" / "schemas" / "media-part.schema.json",
)

VALID = sorted(FIXTURES.glob("valid-*.json"))
INVALID = sorted(FIXTURES.glob("invalid-*.json"))


def _validator(schema_path: Path) -> Draft202012Validator:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_fixture_inventory_is_complete() -> None:
    assert len(VALID) == 6
    assert len(INVALID) == 9


def test_schema_copies_are_in_sync() -> None:
    bodies = []
    for path in SCHEMA_PATHS:
        schema = json.loads(path.read_text(encoding="utf-8"))
        schema.pop("$comment", None)
        bodies.append(schema)
    assert bodies[0] == bodies[1]


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=("root", "packaged"))
@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixtures_pass(schema_path: Path, path: Path) -> None:
    _validator(schema_path).validate(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=("root", "packaged"))
@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixtures_are_rejected(schema_path: Path, path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert not _validator(schema_path).is_valid(document), (
        f"{path.name} unexpectedly validated"
    )


def test_model_call_schema_embeds_media_part_def() -> None:
    """Both model-call schema copies carry the folded MediaPart on ContentPart."""
    for name in ("schemas", "src/novafabric/schemas"):
        schema = json.loads(
            (REPO_ROOT / name / "model-call.schema.json").read_text(encoding="utf-8")
        )
        content_part = schema["$defs"]["ContentPart"]["properties"]
        assert content_part["media"]["$ref"] == "#/$defs/MediaPart"
        media_def = schema["$defs"]["MediaPart"]
        assert media_def["required"] == [
            "type", "media_type", "content_hash", "byte_size", "redacted",
        ]
        assert media_def["additionalProperties"] is False
