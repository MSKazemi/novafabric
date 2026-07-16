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
"""Golden-fixture and model tests for the ADR-0134 retention schemas."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from novafabric.retention.models import (
    RetentionActionRecord,
    RetentionBinding,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "retention-scheduler"


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / name).read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _fixtures(prefix: str) -> list[Path]:
    paths = sorted(FIXTURE_DIR.glob(f"{prefix}-*.json"))
    assert paths, f"no fixtures with prefix {prefix!r} in {FIXTURE_DIR}"
    return paths


# ---------------------------------------------------------------------------
# All 17 golden fixtures behave as their filename asserts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _fixtures("binding"), ids=lambda p: p.stem)
def test_binding_fixture_matches_filename(path: Path) -> None:
    validator = _validator("retention-binding.schema.json")
    data = json.loads(path.read_text())
    errors = list(validator.iter_errors(data))
    if "-valid-" in path.name:
        assert not errors, f"{path.name}: unexpected errors: {errors}"
    else:
        assert errors, f"{path.name}: expected schema errors, got none"


@pytest.mark.parametrize("path", _fixtures("record"), ids=lambda p: p.stem)
def test_record_fixture_matches_filename(path: Path) -> None:
    validator = _validator("retention-action-record.schema.json")
    data = json.loads(path.read_text())
    errors = list(validator.iter_errors(data))
    if "-valid-" in path.name:
        assert not errors, f"{path.name}: unexpected errors: {errors}"
    else:
        assert errors, f"{path.name}: expected schema errors, got none"


def test_all_17_fixtures_present() -> None:
    assert len(list(FIXTURE_DIR.glob("*.json"))) == 17


# ---------------------------------------------------------------------------
# Pydantic models agree with the JSON Schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _fixtures("binding"), ids=lambda p: p.stem)
def test_binding_model_agrees_with_schema(path: Path) -> None:
    data = json.loads(path.read_text())
    if "-valid-" in path.name:
        binding = RetentionBinding.model_validate(data)
        assert binding.id == data["id"]
    else:
        with pytest.raises(ValidationError):
            RetentionBinding.model_validate(data)


@pytest.mark.parametrize("path", _fixtures("record"), ids=lambda p: p.stem)
def test_record_model_agrees_with_schema(path: Path) -> None:
    data = json.loads(path.read_text())
    if "-valid-" in path.name:
        record = RetentionActionRecord.model_validate(data)
        assert record.item_id == data["item_id"]
    else:
        with pytest.raises(ValidationError):
            RetentionActionRecord.model_validate(data)


def test_record_json_dump_is_schema_valid() -> None:
    """model_dump(mode='json', exclude_none=True) round-trips through the schema."""
    record = RetentionActionRecord(
        swept_at=datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc),
        principal="cron://nightly-retention",
        registry="reg",
        binding_id="b1",
        item_kind="capsule",  # type: ignore[arg-type]
        item_id="cap-1",
        action="purge",  # type: ignore[arg-type]
        outcome="skipped",  # type: ignore[arg-type]
        due_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        reason="worm_hold",
        worm_locked_until=datetime(2028, 1, 1, tzinfo=timezone.utc),
    )
    validator = _validator("retention-action-record.schema.json")
    dumped = record.model_dump(mode="json", exclude_none=True)
    errors = list(validator.iter_errors(dumped))
    assert not errors, errors
