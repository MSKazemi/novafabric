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
"""Regulator-framework to test-case mapping (FR-12).

Each framework maps to a subset (or full set) of the 10 mandated test cases.
"""

from __future__ import annotations

# All 11 mandated test case identifiers (FR-11, FR-15)
ALL_TEST_CASES = [
    "test_root_cannot_delete_locked_object",
    "test_retention_cannot_be_shortened",
    "test_retention_can_be_extended",
    "test_legal_hold_blocks_deletion_after_retention_expiry",
    "test_legal_hold_release_required_before_deletion",
    "test_lifecycle_expiration_silently_skipped_on_locked_object",
    "test_compliance_mode_cannot_be_disabled_after_bucket_creation",
    "test_governance_bypass_does_not_apply_in_compliance_mode",
    "test_overwrite_in_place_rejected_for_locked_object",
    "test_conditional_put_returns_412_on_existing_key",
    # Test 11 (BQ-013, AC-4): FR-15 — in-flight corruption detection via SHA-256 checksum
    "test_sha256_checksum_enforced_by_backend",
]

# Framework descriptions
FRAMEWORK_DESCRIPTIONS = {
    "sec-17a-4": "SEC Rule 17a-4(f) — WORM records for broker-dealers",
    "mifid-ii":  "MiFID II Article 16 — Investment firm record retention",
    "cftc-1.31": "CFTC Regulation 1.31 — Commodity trading records",
    "finra-4370": "FINRA Rule 4370 / 4511 — Member firm books and records",
    "fda-21cfr-11": "FDA 21 CFR Part 11 — Electronic records for pharma/clinical",
}

# Framework → required test cases
# All 10 are required for all frameworks (complete WORM compliance is the baseline).
# A framework may have additional emphasis on certain tests (documented in COMPATIBILITY.md).
FRAMEWORK_TEST_CASES: dict[str, list[str]] = {
    framework: list(ALL_TEST_CASES)
    for framework in FRAMEWORK_DESCRIPTIONS
}

VALID_FRAMEWORKS = list(FRAMEWORK_DESCRIPTIONS.keys())
VALID_BACKENDS = ["s3", "minio", "ceph_rgw", "azure_blob"]


def get_test_cases(framework: str) -> list[str]:
    """Return the list of required test case names for *framework*."""
    if framework not in FRAMEWORK_TEST_CASES:
        raise ValueError(
            f"Unknown framework {framework!r}. Valid: {', '.join(VALID_FRAMEWORKS)}"
        )
    return FRAMEWORK_TEST_CASES[framework]
