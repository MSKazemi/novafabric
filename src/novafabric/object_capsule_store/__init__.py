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
"""Object Capsule Store — Phase 4 implementation.

Capabilities:
    cap-001: Capsule Manifest Chain (FR-01..FR-05)
    cap-002: Atomic NovaSeal Attachment via Chain Commit (FR-06..FR-09)
    cap-003: WORM Behavioral Test Suite (FR-10..FR-13) — see packages/nova_worm_conformance/
    cap-004: Strict Client-Side CAS Enforcement (FR-14..FR-17)
    cap-005: Zstd Dictionary Compression (ZstdDictRegistry, compression_dict_id)
"""

from novafabric.object_capsule_store.client import ObjectCapsuleStore
from novafabric.object_capsule_store.exceptions import (
    CapsuleHashMismatch,
    CASMismatchError,
    ChainCommitConflict,
    CompressionDictNotFound,
    NovaSealUnavailable,
    WormPolicyViolation,
)
from novafabric.object_capsule_store.models import (
    CapsuleReceipt,
    IntegrityResult,
    ManifestCommit,
    OrphanReport,
    RebuildReport,
    ReceiptStatus,
    WalEntry,
)
from novafabric.object_capsule_store.zstd_dict import ZstdDictRegistry

__all__ = [
    "ObjectCapsuleStore",
    "ZstdDictRegistry",
    "CASMismatchError",
    "CapsuleHashMismatch",
    "ChainCommitConflict",
    "CompressionDictNotFound",
    "NovaSealUnavailable",
    "WormPolicyViolation",
    "CapsuleReceipt",
    "IntegrityResult",
    "ManifestCommit",
    "OrphanReport",
    "RebuildReport",
    "ReceiptStatus",
    "WalEntry",
]
