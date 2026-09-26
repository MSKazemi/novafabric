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

P1 ships the NF-391 KB-mutation ledger and its ``memstore_mutation`` facet.
P2 adds the NF-392 access-governance ledger (``access`` — ``contained: false``
is evidence of an out-of-scope access, never enforcement) and NF-395/396
cross-run derivation (``derivation`` — read → seeding write → origin run, and
source → store-write → later runs), both stored inside the same facet, plus
``nova memstore access ledger | derive | provenance``. Retention conformance
(NF-393), at-rest poisoning markers (NF-394) and NF-397..400 are later slices.
"""

from novafabric.memstore.access import (
    ACCESS_BLOCK,
    IN_MISSION_BOUNDARY,
    AccessLedgerBlock,
    AccessRecord,
    AccessVerified,
    ScopeError,
    ScopeFlagFinding,
    StoreMismatchError,
    access_block_from_capsule,
    access_digest,
    access_head,
    access_kwargs_from_memory_event,
    attach_access,
    build_access_block,
    guard_payload,
    record_access,
    scope_contains,
    uncontained,
    verify_access_block,
    verify_access_chain,
    verify_scope_flags,
)
from novafabric.memstore.derivation import (
    DERIVATION_BLOCK,
    DerivationBoundError,
    DerivationLink,
    DerivationTrace,
    EdgeCrosscheck,
    LedgerAssembly,
    LedgerIndex,
    ProvenanceChain,
    ProvenanceNode,
    StoreEvidence,
    TraceHop,
    assemble_ledger,
    attach_derivation,
    collect_store_evidence,
    crosscheck_read_edges,
    derive_read,
    derive_reads,
    ledger_from_sidecar,
    provenance_chain,
    trace_derivation,
)
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
    "ACCESS_BLOCK",
    "DERIVATION_BLOCK",
    "IN_MISSION_BOUNDARY",
    "AccessLedgerBlock",
    "AccessRecord",
    "AccessVerified",
    "DerivationBoundError",
    "DerivationLink",
    "DerivationTrace",
    "EdgeCrosscheck",
    "LedgerAssembly",
    "LedgerIndex",
    "ProvenanceChain",
    "ProvenanceNode",
    "ScopeError",
    "ScopeFlagFinding",
    "StoreEvidence",
    "StoreMismatchError",
    "TraceHop",
    "access_block_from_capsule",
    "access_digest",
    "access_head",
    "access_kwargs_from_memory_event",
    "assemble_ledger",
    "attach_access",
    "attach_derivation",
    "build_access_block",
    "collect_store_evidence",
    "crosscheck_read_edges",
    "derive_read",
    "derive_reads",
    "guard_payload",
    "ledger_from_sidecar",
    "provenance_chain",
    "record_access",
    "scope_contains",
    "trace_derivation",
    "uncontained",
    "verify_access_block",
    "verify_access_chain",
    "verify_scope_flags",
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
