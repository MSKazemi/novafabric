"""LifecycleEvent record model — schema conformance and hygiene (ADR-0137 D1/D5)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.events.hygiene import sanitize_record
from novafabric.events.model import (
    SCHEMA_VERSION,
    EventType,
    LifecycleEvent,
    Subject,
    SubjectKind,
)

SCHEMA_PATH = (
    Path(__file__).parents[2] / "src/novafabric/schemas/lifecycle-event.schema.json"
)
ROOT_SCHEMA_PATH = Path(__file__).parents[2] / "schemas/lifecycle-event.schema.json"


def _schema() -> dict:  # type: ignore[type-arg]
    return json.loads(SCHEMA_PATH.read_text())


def _event(**overrides: object) -> LifecycleEvent:
    kwargs: dict = {
        "type": EventType.CAPSULE_CREATED,
        "subject": Subject(kind=SubjectKind.CAPSULE, ref="run-abc123",
                           digest="sha256:" + "9f" * 32),
        "payload": {"status": "success", "model_call_count": 7},
        "source": "nova capture",
    }
    kwargs.update(overrides)
    return LifecycleEvent(**kwargs)


class TestLifecycleEventModel:
    def test_defaults(self) -> None:
        event = _event()
        assert event.schema_version == SCHEMA_VERSION
        assert re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", event.event_id)
        # RFC 3339 UTC with microsecond precision.
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", event.occurred_at
        )

    def test_record_validates_against_schema(self) -> None:
        record = _event().to_record()
        jsonschema.validate(
            record, _schema(), format_checker=jsonschema.FormatChecker()
        )

    def test_record_omits_unset_optionals(self) -> None:
        record = LifecycleEvent(
            type=EventType.RETENTION_APPLIED,
            subject=Subject(kind=SubjectKind.RETENTION, ref="run-x9"),
        ).to_record()
        assert "signature" not in record
        assert "source" not in record
        assert "nova.version" not in record
        assert record["subject"]["digest"] is None
        jsonschema.validate(
            record, _schema(), format_checker=jsonschema.FormatChecker()
        )

    def test_nova_version_alias(self) -> None:
        record = _event(nova_version="0.59.0").to_record()
        assert record["nova.version"] == "0.59.0"

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _event(type="CapsuleCreated")

    def test_invalid_subject_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Subject(kind="prompt", ref="x")

    def test_empty_subject_ref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Subject(kind=SubjectKind.CAPSULE, ref="")

    def test_unknown_toplevel_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _event(prompt_text="never")

    def test_taxonomy_matches_schema_enum(self) -> None:
        assert {t.value for t in EventType} == set(_schema()["properties"]["type"]["enum"])

    def test_schema_copies_identical(self) -> None:
        assert SCHEMA_PATH.read_text() == ROOT_SCHEMA_PATH.read_text()


class TestHygiene:
    def test_secret_in_payload_is_masked(self) -> None:
        record = _event(
            payload={"note": "leaked sk-ant-" + "a1B2" * 8 + " key"}
        ).to_record()
        sanitized = sanitize_record(record)
        assert "sk-ant-" not in json.dumps(sanitized)
        assert "[REDACTED:anthropic-api-key]" in sanitized["payload"]["note"]

    def test_digest_survives_sanitization(self) -> None:
        digest = "sha256:" + "9f" * 32
        record = _event().to_record()
        sanitized = sanitize_record(record)
        assert sanitized["subject"]["digest"] == digest

    def test_nested_and_list_values_scanned(self) -> None:
        record = _event(
            payload={"nested": {"tokens": ["hf_" + "Q" * 40, "plain"]}}
        ).to_record()
        sanitized = sanitize_record(record)
        assert sanitized["payload"]["nested"]["tokens"][0] == (
            "[REDACTED:huggingface-token]"
        )
        assert sanitized["payload"]["nested"]["tokens"][1] == "plain"

    def test_non_string_values_untouched(self) -> None:
        record = _event(payload={"count": 7, "flag": True, "none": None}).to_record()
        assert sanitize_record(record)["payload"] == {
            "count": 7, "flag": True, "none": None,
        }
