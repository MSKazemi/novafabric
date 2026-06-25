# nova-worm-conformance

WORM behavioral test suite for NovaFabric-compatible object storage backends.

## Overview

This standalone package implements **cap-003** from the NovaFabric Object
Capsule Store specification — a suite of 10 mandated test cases that verify
a storage backend meets WORM (Write Once, Read Many) requirements for regulated
industries (SEC 17a-4, MiFID II, CFTC 1.31, FINRA 4370, FDA 21 CFR Part 11).

## Installation

```bash
pip install nova-worm-conformance boto3
```

For Azure Blob backends:

```bash
pip install nova-worm-conformance azure-storage-blob
```

## Usage

```bash
nova-worm-test \
  --backend minio \
  --endpoint http://localhost:9000 \
  --bucket my-worm-bucket \
  --framework sec-17a-4 \
  --output report.json
```

Exit code 0 = all tests passed. Exit code 1 = one or more tests failed.

## Test Cases (FR-11)

All 10 test cases are always executed:

1. `test_root_cannot_delete_locked_object`
2. `test_retention_cannot_be_shortened`
3. `test_retention_can_be_extended`
4. `test_legal_hold_blocks_deletion_after_retention_expiry`
5. `test_legal_hold_release_required_before_deletion`
6. `test_lifecycle_expiration_silently_skipped_on_locked_object`
7. `test_compliance_mode_cannot_be_disabled_after_bucket_creation`
8. `test_governance_bypass_does_not_apply_in_compliance_mode`
9. `test_overwrite_in_place_rejected_for_locked_object`
10. `test_conditional_put_returns_412_on_existing_key`

## Signing Reports (FR-13)

Use `--sign` to attach a NovaSeal signature to the report:

```bash
nova-worm-test ... --sign --output signed-report.json
```

The `novaseal_signature` field in the JSON report is independently verifiable.

## Compatibility

See [COMPATIBILITY.md](COMPATIBILITY.md) for backend-version compatibility matrix.

## License

Apache-2.0
