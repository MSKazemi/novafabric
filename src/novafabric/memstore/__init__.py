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

"""Persistent-knowledge & organisational-memory governance evidence
(ADR-0171, experimental).

Record-only and store-external: NovaFabric records evidence *about* a shared
knowledge base — who changed which entry, what changed, and when. It does not
host, serve, manage, read, or write the store, and it never asserts that a
mutation was authorised, correct, or benign.

P1 ships the NF-391 KB-mutation ledger and its ``memstore_mutation`` facet only.
The access-governance ledger (NF-392), retention conformance (NF-393), at-rest
poisoning markers (NF-394), cross-run derivation (NF-395/396) and the rest of
the cluster are later slices. There is no CLI yet.
"""

from novafabric.memstore.ledger import (
    FACET_NAME,
    MAX_ID_LENGTH,
    SCHEMA_VERSION,
    ContentCaptureError,
    InvalidDigestError,
    LedgerRewriteError,
    LedgerVerification,
    MemstoreError,
    MemstoreMutationFacet,
    MutationActor,
    MutationOp,
    MutationRecord,
    append_mutation,
    attach_facet,
    build_facet,
    chain_head,
    digest_value,
    facet_for_ledger,
    facet_from_capsule,
    record_digest,
    record_preimage,
    scan_for_content,
    verify_append_only,
    verify_chain,
    verify_facet_binding,
)

__all__ = [
    "FACET_NAME",
    "MAX_ID_LENGTH",
    "SCHEMA_VERSION",
    "ContentCaptureError",
    "InvalidDigestError",
    "LedgerRewriteError",
    "LedgerVerification",
    "MemstoreError",
    "MemstoreMutationFacet",
    "MutationActor",
    "MutationOp",
    "MutationRecord",
    "append_mutation",
    "attach_facet",
    "build_facet",
    "chain_head",
    "digest_value",
    "facet_for_ledger",
    "facet_from_capsule",
    "record_digest",
    "record_preimage",
    "scan_for_content",
    "verify_append_only",
    "verify_chain",
    "verify_facet_binding",
]
