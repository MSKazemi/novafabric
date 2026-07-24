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

"""Batch import / instance interchange (ADR-0207) — the inverse of ``export_blob``.

``import_batch`` ingests an ADR-0141 batch export into the local capsule store:
verify → unpack → reindex → receipt. Fail-closed, idempotent, local-first.
"""

from novafabric.import_blob.models import (
    RECEIPT_SCHEMA_VERSION,
    ImportCounts,
    ImportReceipt,
    MemberRecord,
    ReindexInfo,
    VerificationInfo,
)
from novafabric.import_blob.service import (
    ImportOutcome,
    ImportUsageError,
    import_batch,
)
from novafabric.import_blob.unpack import UnpackError, safe_extract_tar

__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "ImportCounts",
    "ImportOutcome",
    "ImportReceipt",
    "ImportUsageError",
    "MemberRecord",
    "ReindexInfo",
    "UnpackError",
    "VerificationInfo",
    "import_batch",
    "safe_extract_tar",
]
