"""Capsule format migration utilities (v0.1.x → v1.0.0)."""

from novafabric.capsule_migration._converter import (
    CapsuleMigrationError,
    CapsuleMigrator,
    MigrationResult,
)

__all__ = ["CapsuleMigrationError", "CapsuleMigrator", "MigrationResult"]
