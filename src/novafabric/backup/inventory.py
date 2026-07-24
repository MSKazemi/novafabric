"""Read-only backup-set inventory (ADR-0201 P7 dashboard status surface).

``list_backup_sets`` scans a directory for ``nova-backup-*.tar.gz`` archives
and summarises each from its embedded ``manifest.json`` — set_id, profile,
member/byte counts, signing status — **without** extracting members or
verifying hashes/signatures. It is a cheap listing, not a
:func:`novafabric.backup.verify.verify_backup` (which opens and hashes every
member); a summary here therefore says nothing about integrity, only about
what the manifest claims. An archive that is unreadable, lacks a manifest, or
whose manifest fails the closed schema is reported with ``ok=False`` and a
reason rather than skipped silently — an unlistable backup is a fact worth
showing, not a fact to hide.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from pydantic import BaseModel, ValidationError

from novafabric.backup.create import MANIFEST_NAME
from novafabric.backup.models import BackupManifest

#: Default cap on the number of archives summarised in one listing, so a
#: directory with thousands of sets cannot produce an unbounded response.
DEFAULT_LIMIT = 200

_ARCHIVE_GLOB = "nova-backup-*.tar.gz"


class BackupSetSummary(BaseModel):
    """One archive's manifest-claimed summary (no integrity assertion)."""

    filename: str
    ok: bool
    set_id: str | None = None
    created_at: str | None = None
    profile: str | None = None
    nova_version: str | None = None
    member_count: int | None = None
    member_bytes: int | None = None
    archive_bytes: int | None = None
    signing_status: str | None = None
    error: str | None = None


def _summarize(archive: Path) -> BackupSetSummary:
    try:
        archive_bytes = archive.stat().st_size
    except OSError:
        archive_bytes = None
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.extractfile(MANIFEST_NAME)
            if member is None:
                return BackupSetSummary(
                    filename=archive.name,
                    ok=False,
                    archive_bytes=archive_bytes,
                    error=f"no {MANIFEST_NAME} in archive",
                )
            raw = member.read()
        manifest = BackupManifest.model_validate(json.loads(raw))
    except (tarfile.TarError, OSError, json.JSONDecodeError, ValidationError, KeyError) as exc:
        return BackupSetSummary(
            filename=archive.name,
            ok=False,
            archive_bytes=archive_bytes,
            error=f"{type(exc).__name__}: {exc}",
        )
    return BackupSetSummary(
        filename=archive.name,
        ok=True,
        set_id=manifest.set_id,
        created_at=manifest.created_at,
        profile=manifest.profile,
        nova_version=manifest.nova_version,
        member_count=len(manifest.members),
        member_bytes=sum(m.size_bytes for m in manifest.members),
        archive_bytes=archive_bytes,
        signing_status=manifest.signing_status,
    )


def list_backup_sets(
    directory: Path, *, limit: int = DEFAULT_LIMIT
) -> tuple[list[BackupSetSummary], bool]:
    """Summarise the backup archives in *directory* (non-recursive).

    Returns ``(summaries, truncated)``. Summaries are ordered by
    ``created_at`` descending (newest first); unreadable archives sort last
    with an empty key. ``truncated`` is True when more than *limit* archives
    were present and the tail was dropped. A missing directory yields
    ``([], False)`` — an honest "no backups here", not an error.
    """
    if not directory.is_dir():
        return [], False
    archives = sorted(directory.glob(_ARCHIVE_GLOB))
    summaries = [_summarize(a) for a in archives]
    summaries.sort(key=lambda s: s.created_at or "", reverse=True)
    truncated = len(summaries) > limit
    return summaries[:limit], truncated
