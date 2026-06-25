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
"""Smoke tests for nova_worm_conformance package (FR-10..FR-13)."""

from __future__ import annotations

import importlib
import json

import pytest
from nova_worm_conformance.frameworks import (
    ALL_TEST_CASES,
    VALID_BACKENDS,
    VALID_FRAMEWORKS,
    get_test_cases,
)
from nova_worm_conformance.report import TestRecord, build_report

# ---------------------------------------------------------------------------
# FR-11: Exactly 10 test cases defined
# ---------------------------------------------------------------------------

def test_exactly_10_test_cases():
    """FR-11: Exactly 10 mandated test cases — no subset."""
    assert len(ALL_TEST_CASES) == 10


def test_all_test_case_modules_importable():
    """FR-11: All 10 test case modules can be imported."""
    for test_name in ALL_TEST_CASES:
        module_name = f"nova_worm_conformance.tests.{test_name}"
        module = importlib.import_module(module_name)
        assert hasattr(module, "run"), f"{module_name} missing run() function"


def test_all_frameworks_return_10_tests():
    """FR-11: All frameworks require all 10 tests."""
    for framework in VALID_FRAMEWORKS:
        tests = get_test_cases(framework)
        assert len(tests) == 10, f"Framework {framework} has {len(tests)} tests, expected 10"
        assert set(tests) == set(ALL_TEST_CASES)


def test_invalid_framework_raises():
    with pytest.raises(ValueError, match="Unknown framework"):
        get_test_cases("unknown-framework")


# ---------------------------------------------------------------------------
# FR-12: COMPATIBILITY.md exists
# ---------------------------------------------------------------------------

def test_compatibility_md_exists():
    from pathlib import Path

    compat_path = Path(__file__).parent.parent / "COMPATIBILITY.md"
    assert compat_path.exists(), "COMPATIBILITY.md must exist in nova_worm_conformance package"
    content = compat_path.read_text()
    assert "sec-17a-4" in content
    assert "minio" in content.lower()


# ---------------------------------------------------------------------------
# Report serialisation
# ---------------------------------------------------------------------------

def test_build_report_serialisation():
    """ConformanceReport.to_json() round-trips correctly."""
    record = TestRecord(
        test_name="test_conditional_put_returns_412_on_existing_key",
        passed=True,
        framework="sec-17a-4",
        backend="minio",
        backend_version="RELEASE.2024",
        timestamp="2026-05-12T00:00:00Z",
    )
    report = build_report(
        backend="minio",
        endpoint="http://localhost:9000",
        bucket="test-bucket",
        framework="sec-17a-4",
        records=[record],
        started_at="2026-05-12T00:00:00Z",
    )
    assert report.total == 1
    assert report.passed == 1
    assert report.failed == 0
    assert report.all_passed is True

    serialised = json.loads(report.to_json())
    assert serialised["total"] == 1
    assert serialised["all_passed"] is True
    assert len(serialised["records"]) == 1
    assert serialised["novaseal_signature"] is None


def test_report_with_failures():
    """Failed tests → all_passed=False, exit_code=1."""
    record = TestRecord(
        test_name="test_root_cannot_delete_locked_object",
        passed=False,
        framework="mifid-ii",
        backend="s3",
        backend_version="2024",
        timestamp="2026-05-12T00:00:00Z",
        error="Deletion succeeded (unexpected)",
    )
    report = build_report(
        backend="s3",
        endpoint="https://s3.amazonaws.com",
        bucket="bucket",
        framework="mifid-ii",
        records=[record],
        started_at="2026-05-12T00:00:00Z",
    )
    assert report.all_passed is False
    assert report.failed == 1


# ---------------------------------------------------------------------------
# FR-10: CLI imports cleanly
# ---------------------------------------------------------------------------

def test_cli_importable():
    from nova_worm_conformance.cli import app

    assert app is not None


# ---------------------------------------------------------------------------
# FR-10: Valid backends list
# ---------------------------------------------------------------------------

def test_valid_backends():
    assert "minio" in VALID_BACKENDS
    assert "s3" in VALID_BACKENDS
    assert "ceph_rgw" in VALID_BACKENDS
    assert "azure_blob" in VALID_BACKENDS


# ---------------------------------------------------------------------------
# Schema validation of manifest_commit_v1.schema.json
# ---------------------------------------------------------------------------

def test_manifest_commit_schema_valid():
    """JSON Schema artifacts validate via Draft202012Validator.check_schema()."""
    from pathlib import Path

    import jsonschema

    schema_path = (
        Path(__file__).parent.parent.parent.parent
        / "src/novafabric/object_capsule_store/schemas/manifest_commit_v1.schema.json"
    )
    if not schema_path.exists():
        pytest.skip("Schema file not found at expected path")

    schema = json.loads(schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)


def test_checkpoint_schema_valid():
    """checkpoint_v1.schema.json validates via Draft202012Validator.check_schema()."""
    from pathlib import Path

    import jsonschema

    schema_path = (
        Path(__file__).parent.parent.parent.parent
        / "src/novafabric/object_capsule_store/schemas/checkpoint_v1.schema.json"
    )
    if not schema_path.exists():
        pytest.skip("Schema file not found at expected path")

    schema = json.loads(schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
