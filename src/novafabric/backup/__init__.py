"""Evidence-grade backup sets and restore (ADR-0181, experimental).

First slice: ``nova backup create`` / ``nova backup verify`` for the local
``~/.novafabric`` layout. Second slice: ``nova restore`` (local profile, with
normative crypto-shred replay and a closing verification chain) plus the
``pg`` create profile (``pg_dump --format=custom`` member). Restore of a
pg-dump set remains the ``pg_restore`` runbook
(``docs/ops/backup-restore.md`` §1.2); see ``design/spec/backup-restore-v0.md``.
"""

from novafabric.backup.create import (
    BackupCreateError,
    BackupCreateResult,
    PgDumpNotFoundError,
    create_backup,
)
from novafabric.backup.models import (
    BackupManifest,
    BackupMember,
    ManifestSignature,
    MemberCheck,
    RestoreResult,
    RestoreStepResult,
    VerifyResult,
)
from novafabric.backup.restore import RestoreError, restore_backup
from novafabric.backup.verify import BackupVerifyError, verify_backup

__all__ = [
    "BackupCreateError",
    "BackupCreateResult",
    "BackupManifest",
    "BackupMember",
    "BackupVerifyError",
    "ManifestSignature",
    "MemberCheck",
    "PgDumpNotFoundError",
    "RestoreError",
    "RestoreResult",
    "RestoreStepResult",
    "VerifyResult",
    "create_backup",
    "restore_backup",
    "verify_backup",
]
