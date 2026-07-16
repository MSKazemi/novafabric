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

"""Batch capsule blob export with a signed completeness manifest (ADR-0141).

Selects a set of run capsules, writes each one content-addressed to a blob
destination through the existing storage adapters, and emits one NovaSeal/DSSE-
signed ``export-manifest.json`` — the authority on batch membership, integrity,
and completeness. Verification is offline and total.

Reuses (never reimplements):

- CAS addressing — :func:`novafabric.object_capsule_store.cas.compute_sha256` (ADR-0103)
- WORM S3 writes — :class:`novafabric.storage._s3_worm.S3WormAdapter` (ADR-0031/0062)
- DSSE signing — :func:`novafabric.evidence.intoto.dsse_sign_payload` (ADR-0041 path)
- Ed25519 keys — :mod:`novafabric.evidence.signing` / :mod:`novafabric.trust.keyring`
"""

from novafabric.export_blob.digest import canonical_signing_payload, compute_batch_digest
from novafabric.export_blob.models import (
    MANIFEST_FILENAME,
    MANIFEST_PAYLOAD_TYPE,
    SCHEMA_VERSION,
    ExportManifest,
    ExportMember,
    WormIntent,
)
from novafabric.export_blob.service import (
    ExportResult,
    VerifyReport,
    VerifyStatus,
    export_batch,
    select_capsules,
    verify_export_manifest,
)

__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_PAYLOAD_TYPE",
    "SCHEMA_VERSION",
    "ExportManifest",
    "ExportMember",
    "ExportResult",
    "VerifyReport",
    "VerifyStatus",
    "WormIntent",
    "canonical_signing_payload",
    "compute_batch_digest",
    "export_batch",
    "select_capsules",
    "verify_export_manifest",
]
