# WORM Conformance Compatibility Matrix

This document tracks which backend versions have been tested against the 10
mandated WORM conformance test cases (cap-003, FR-11, FR-12).

## Test Cases

| # | Test Case | Required by |
|---|-----------|-------------|
| 1 | `test_root_cannot_delete_locked_object` | All frameworks |
| 2 | `test_retention_cannot_be_shortened` | All frameworks |
| 3 | `test_retention_can_be_extended` | All frameworks |
| 4 | `test_legal_hold_blocks_deletion_after_retention_expiry` | All frameworks |
| 5 | `test_legal_hold_release_required_before_deletion` | All frameworks |
| 6 | `test_lifecycle_expiration_silently_skipped_on_locked_object` | All frameworks |
| 7 | `test_compliance_mode_cannot_be_disabled_after_bucket_creation` | All frameworks |
| 8 | `test_governance_bypass_does_not_apply_in_compliance_mode` | All frameworks |
| 9 | `test_overwrite_in_place_rejected_for_locked_object` | All frameworks |
| 10 | `test_conditional_put_returns_412_on_existing_key` | All frameworks |

## Backend Compatibility

| Backend | Version | Tests 1-10 | Notes |
|---------|---------|------------|-------|
| AWS S3 | Latest | Pending | Object Lock COMPLIANCE mode required at bucket creation |
| MinIO | RELEASE.2024-* | Pending | Erasure mode required for Object Lock; `--console-address` flag |
| Ceph RGW | Pacific (v16.2+) | Pending | `rgw_enable_bucket_versioning` must be true |
| Azure Blob | SDK 12.23+ | Partial | Tests 7-8 use different API surface; see notes below |

### AWS S3 Notes

- Object Lock must be enabled at bucket creation; cannot be enabled retroactively.
- COMPLIANCE mode is enforced by the S3 service; root account cannot bypass.
- Legal holds (test 4, 5) are independent of retention mode.
- `IfNoneMatch: *` conditional PUT (test 10) supported since 2024.

### MinIO Notes

- MinIO server (AGPL-3.0) must be deployed as a separate network service.
- Object Lock requires Erasure Set mode (not single-drive mode).
- Governance bypass test (test 8) requires appropriate IAM policy.

### Ceph RGW Notes

- Object Lock requires `--rgw-enable-bucket-versioning=true` in Ceph config.
- Legal hold behavior matches S3 API specification.

### Azure Blob Notes

- Azure uses container-level or version-level immutability policies.
- Tests using `x-amz-*` headers are adapted to Azure SDK equivalents.
- Test 10 uses Azure's `If-None-Match: *` equivalent.
- GCS backend: deferred to v0.3 (cap-007).

## Regulatory Frameworks

| Framework | Description | Required Tests |
|-----------|-------------|----------------|
| `sec-17a-4` | SEC Rule 17a-4(f) — broker-dealer records | All 10 |
| `mifid-ii` | MiFID II Article 16 — investment firm records | All 10 |
| `cftc-1.31` | CFTC Regulation 1.31 — commodity trading records | All 10 |
| `finra-4370` | FINRA Rule 4370/4511 — member firm records | All 10 |
| `fda-21cfr-11` | FDA 21 CFR Part 11 — electronic records | All 10 |

All 10 tests are required for every framework — partial compliance is not
sufficient for regulated-industry deployments.
