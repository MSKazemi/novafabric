"""novafabric.metadata_store — run/capsule/signature indexing tier.

The metadata store is a DERIVED INDEX over the object capsule store (ADR-0022).
It is never the source of truth and is fully rebuildable from raw capsules.

Public API
----------
MetadataStore            — abstract base class; all implementations must satisfy this.
RLSContextMissing        — raised when begin_tenant_context() lacks an open transaction.
BackendModeError         — raised when a backend is used in an unsupported configuration.
MigrationVerificationFailed — raised when migrate-to-postgres verification fails.
get_metadata_store()     — factory; reads NOVAFABRIC_METADATA_BACKEND env var.
"""
from novafabric.metadata_store.factory import get_metadata_store
from novafabric.metadata_store.interface import (
    BackendModeError,
    MetadataStore,
    MigrationVerificationFailed,
    RLSContextMissing,
)

__all__ = [
    "MetadataStore",
    "RLSContextMissing",
    "BackendModeError",
    "MigrationVerificationFailed",
    "get_metadata_store",
]
