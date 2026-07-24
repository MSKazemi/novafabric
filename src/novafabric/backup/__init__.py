"""Evidence-grade backup sets and restore (ADR-0181/0216/0217, experimental).

``nova backup create`` / ``nova backup verify`` / ``nova restore``. The
``local-full`` profile covers every persistent local store with a signed
coverage table (ADR-0216); key material travels only under the
``--include-keys``/``--restore-keys`` dual opt-in. ``nova restore`` dispatches
on the verified manifest's profile: ``pg-dump`` sets restore automatically
(safety dump, single-transaction ``pg_restore``, alembic-to-head,
manifest-anchored row counts, RLS proof — ADR-0217; the schema-skew guard and
``nova db upgrade --track`` disambiguation are ADR-0211 Part B), and
``manifest-only`` sets verify chain heads against the live WORM bucket and
rebuild the metadata DB (ADR-0216 D6). Specs:
``design/spec/backup-restore-v0.md``,
``design/spec/pg-restore-skew-guard-v0.md``.
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
    CoverageEntry,
    ManifestSignature,
    MemberCheck,
    RestoreResult,
    RestoreStepResult,
    VerifyResult,
)
from novafabric.backup.restore import (
    PgRestoreNotFoundError,
    RestoreError,
    restore_backup,
)
from novafabric.backup.restore_manifest import BucketUnreachableError
from novafabric.backup.verify import BackupVerifyError, verify_backup

__all__ = [
    "BackupCreateError",
    "BackupCreateResult",
    "BackupManifest",
    "BackupMember",
    "BackupVerifyError",
    "BucketUnreachableError",
    "CoverageEntry",
    "ManifestSignature",
    "MemberCheck",
    "PgDumpNotFoundError",
    "PgRestoreNotFoundError",
    "RestoreError",
    "RestoreResult",
    "RestoreStepResult",
    "VerifyResult",
    "create_backup",
    "restore_backup",
    "verify_backup",
]
