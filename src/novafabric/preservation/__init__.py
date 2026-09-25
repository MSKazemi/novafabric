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

"""Evidence longevity & long-term preservation (ADR-0165, experimental).

Record-only: NovaFabric records that preservation happened — a fixity check
ran, a custody hop occurred, a seal was renewed. It does not provide archival
storage, run a Timestamp Authority, convert formats, generate keys, repair bit
rot, or guarantee that any archive is durable, lawful, or regulator-accepted.

P1 ships the NF-331 anchor and the NF-335 fixity log; P2 ships the NF-332
format-migration chain (``format_migration.py``, CLI ``nova migrate-format``),
which records migration hops and walks them offline back to ``original_root``
but never runs a migrator or rewrites a stored capsule. P3's record-only half
ships the NF-333 crypto re-seal event record and the NF-334 LTV renewal chain
(``reseal.py``, CLI ``nova preservation``) — it records and verifies the
events, never generates keys, calls a TSA, or performs a PQC signature.
Conformance, obsolescence and custody (NF-336/337/338) and the whole-chain
re-verification receipt (NF-339/340) are later slices.
"""

from novafabric.preservation.anchor import (
    FACET_NAME,
    MAX_REF_LENGTH,
    SCHEMA_VERSION,
    Fixity,
    FixityAlg,
    FixityCheck,
    FixityLogRewriteError,
    FixityStatus,
    InvalidDigestError,
    PayloadCaptureError,
    PreservationError,
    PreservationFacet,
    ProvenanceEvent,
    append_fixity_check,
    append_provenance_event,
    attach_facet,
    build_anchor,
    check_fixity,
    detected_bit_rot,
    digest_artifact,
    facet_from_capsule,
    fixity_status,
    provenance_event,
    scan_for_payloads,
    verify_anchor_binding,
    verify_append_only,
)
from novafabric.preservation.format_migration import (
    CHAIN_FIELD,
    MIGRATION_EVENT,
    BrokenMigrationChainError,
    ChainFinding,
    ChainFindingCode,
    FormatMigrationHop,
    FormatMigrationRewriteError,
    FormatMigrationVerification,
    InvalidFormatVersionError,
    append_format_migration,
    chain_from_facet,
    parse_format_version,
    plan_next_hop,
    verify_format_migration_chain,
    verify_migration_append_only,
)
from novafabric.preservation.reseal import (
    CRYPTO_MIGRATION_FIELD,
    HASH_STRENGTH,
    LTV_CHAIN_FIELD,
    SIGNATURE_STRENGTH,
    BrokenLtvChainError,
    BrokenResealRecordError,
    CryptoMigrationEvent,
    LtvRenewal,
    LtvVerification,
    OriginalSignatureDroppedError,
    P3RewriteError,
    ResealVerification,
    append_crypto_migration,
    append_ltv_renewal,
    crypto_migrations_from_facet,
    ltv_chain_from_facet,
    plan_ltv_renewal,
    plan_reseal,
    verify_crypto_migrations,
    verify_ltv_chain,
    verify_p3_append_only,
)

__all__ = [
    "CHAIN_FIELD",
    "CRYPTO_MIGRATION_FIELD",
    "FACET_NAME",
    "HASH_STRENGTH",
    "LTV_CHAIN_FIELD",
    "MAX_REF_LENGTH",
    "MIGRATION_EVENT",
    "SCHEMA_VERSION",
    "SIGNATURE_STRENGTH",
    "BrokenLtvChainError",
    "BrokenMigrationChainError",
    "BrokenResealRecordError",
    "ChainFinding",
    "ChainFindingCode",
    "CryptoMigrationEvent",
    "Fixity",
    "FixityAlg",
    "FixityCheck",
    "FixityLogRewriteError",
    "FixityStatus",
    "FormatMigrationHop",
    "FormatMigrationRewriteError",
    "FormatMigrationVerification",
    "InvalidDigestError",
    "InvalidFormatVersionError",
    "LtvRenewal",
    "LtvVerification",
    "OriginalSignatureDroppedError",
    "P3RewriteError",
    "PayloadCaptureError",
    "PreservationError",
    "PreservationFacet",
    "ProvenanceEvent",
    "ResealVerification",
    "append_crypto_migration",
    "append_fixity_check",
    "append_format_migration",
    "append_ltv_renewal",
    "append_provenance_event",
    "attach_facet",
    "build_anchor",
    "chain_from_facet",
    "check_fixity",
    "crypto_migrations_from_facet",
    "detected_bit_rot",
    "digest_artifact",
    "facet_from_capsule",
    "fixity_status",
    "ltv_chain_from_facet",
    "parse_format_version",
    "plan_ltv_renewal",
    "plan_next_hop",
    "plan_reseal",
    "provenance_event",
    "scan_for_payloads",
    "verify_anchor_binding",
    "verify_append_only",
    "verify_crypto_migrations",
    "verify_format_migration_chain",
    "verify_ltv_chain",
    "verify_migration_append_only",
    "verify_p3_append_only",
]
