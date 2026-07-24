"""Typed models for backup-set manifests and verification results (ADR-0181).

The manifest schema follows ``design/spec/backup-restore-v0.md`` with the
first-slice ``local-full`` profile: the local SQLite deployment has no separate
object-store manifest (capsule blobs travel in the set), so
``object_store_manifest`` is ``None`` and ``db_dump`` points at the SQLite
registry snapshot. The schema is CLOSED — unknown manifest keys are rejected on
verify, per the spec.
"""

from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

#: Manifest schema revision (spec header "Schema version").
#: 0.1.1 adds the optional ``db_target`` field (pg-dump profile, second slice).
#: 0.2.0 (ADR-0216/0217) adds member ``role``/``origin``/``sensitive``, the
#: signed ``coverage`` list, ``includes_keys``, the pg verification fields
#: (``pg_schema_revision``/``pg_table_counts``/``pg_client_version``), and the
#: object-store fields (``object_store_backend``/``object_store_fingerprint``).
#: All additions default, so 0.1.x manifests still validate; the schema stays
#: closed, so pre-0.2.0 binaries reject 0.2.0 manifests (deliberate).
MANIFEST_SCHEMA_VERSION = "0.2.0"

#: First-slice profile: local deployment, blobs included (no WORM delegation).
PROFILE_LOCAL_FULL = "local-full"

#: Second-slice profile: server deployment — ``pg_dump --format=custom`` member.
PROFILE_PG_DUMP = "pg-dump"

#: ADR-0181 D2 second profile (wired by ADR-0216 D6): chain heads + checkpoint
#: refs against a WORM object store — hashes travel, blobs stay in the bucket.
PROFILE_MANIFEST_ONLY = "manifest-only"

MemberKind = Literal[
    "db_dump",
    "object_manifest",
    "config",
    "registry",
    "blob",
    "state_db",
    "log",
    "key_material",
]

#: Where a member's bytes came from / where restore puts them back. ``home``
#: members map 1:1 under the NovaFabric home (0.1.x behaviour); the rest are
#: archived under ``external/<origin>/`` and restored to their real roots.
MemberOrigin = Literal["home", "audit", "keyring", "seal-config"]

#: Coverage row status (ADR-0216 D2): what a set does NOT contain is evidence.
CoverageStatus = Literal["included", "absent", "skipped", "excluded"]


class BackupMember(BaseModel):
    """One file in the backup set, addressed by its in-archive path."""

    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str
    size_bytes: int
    kind: MemberKind
    #: Component name from the coverage table (None for 0.1.x sets).
    role: Optional[str] = None
    origin: MemberOrigin = "home"
    #: Sensitive members (dek.db, ratchet state, key material) are restored
    #: with mode 0600 (dirs 0700) — ADR-0216 D3.
    sensitive: bool = False


class CoverageEntry(BaseModel):
    """One component's coverage outcome — part of the signed manifest body."""

    model_config = ConfigDict(extra="forbid")

    component: str
    status: CoverageStatus
    detail: Optional[str] = None


class ManifestSignature(BaseModel):
    """DSSE signature block per the spec: ``{key_id, algorithm, sig}``."""

    model_config = ConfigDict(extra="forbid")

    key_id: str
    algorithm: str
    sig: str


class BackupManifest(BaseModel):
    """Signed (or honestly-unsigned) manifest — the unit of trust of a set."""

    model_config = ConfigDict(extra="forbid")

    manifest_schema_version: str = MANIFEST_SCHEMA_VERSION
    set_id: str
    created_at: str
    profile: str = PROFILE_LOCAL_FULL
    nova_version: str
    schema_revision: Optional[str] = None
    members: list[BackupMember]
    db_dump: Optional[str] = None
    object_store_manifest: Optional[str] = None
    redacted_config: Optional[str] = None
    #: pg-dump profile only: REDACTED dump target, ``host/dbname`` — never a DSN,
    #: never credentials (ADR-0181 D3 hygiene applies to connection strings too).
    db_target: Optional[str] = None
    #: ADR-0216 D2: one row per component; absences are signed, never silent.
    coverage: list[CoverageEntry] = []
    #: ADR-0216 D4: True only when key material was explicitly opted in.
    includes_keys: bool = False
    #: ADR-0217 D5: dump-time alembic revision of the Postgres metadata store.
    pg_schema_revision: Optional[str] = None
    #: ADR-0217 D5: dump-time ``SELECT count(*)`` per core table.
    pg_table_counts: Optional[dict[str, int]] = None
    #: ADR-0217 D6: ``pg_dump --version`` at dump time (skew diagnostics).
    pg_client_version: Optional[str] = None
    #: ADR-0216 D6 (manifest-only profile): backend tag only, e.g. ``"s3"``.
    object_store_backend: Optional[str] = None
    #: ADR-0216 D6: SHA-256 of the canonical secret-free backend fingerprint.
    object_store_fingerprint: Optional[str] = None
    signing_status: Literal["signed", "unsigned"] = "unsigned"
    signing_detail: Optional[str] = None
    signature: Optional[ManifestSignature] = None

    def signable_bytes(self) -> bytes:
        """Canonical manifest body the DSSE signature covers.

        Excludes the signing fields themselves (a signature cannot cover
        itself); sorted keys + compact separators make the encoding
        deterministic across creator and verifier.
        """
        body = self.model_dump(mode="json")
        for field in ("signing_status", "signing_detail", "signature"):
            body.pop(field, None)
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


class RestoreStepResult(BaseModel):
    """One step of the normative restore procedure (spec §Restore procedure)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    detail: str


class RestoreResult(BaseModel):
    """Outcome of ``nova restore`` — ok only when the verification chain passed.

    ``steps`` follows the spec's normative order: verify-set, prepare-home,
    extract, migrations, crypto-shred-replay, verify-storage, verify-seal-log.
    A failure at any step is a failed restore, never a warning.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    set_id: str
    profile: str
    home: str
    steps: list[RestoreStepResult]
    #: Where pre-existing data was moved when ``--force`` displaced it, if any.
    moved_aside: Optional[str] = None


class MemberCheck(BaseModel):
    """Per-member verification outcome."""

    model_config = ConfigDict(extra="forbid")

    path: str
    status: Literal["ok", "mismatch", "missing"]
    expected_sha256: str
    actual_sha256: Optional[str] = None


class VerifyResult(BaseModel):
    """Whole-set verification outcome — any mismatch fails the whole set."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    set_id: str
    profile: str
    checks: list[MemberCheck]
    signing_status: Literal["signed", "unsigned"]
    signature_verified: Optional[bool] = None  # None when the set is unsigned
    errors: list[str] = []
    #: Mirrors the manifest (ADR-0216 D4) so the CLI can warn on key material.
    includes_keys: bool = False

    @property
    def ok_members(self) -> list[MemberCheck]:
        return [c for c in self.checks if c.status == "ok"]

    @property
    def mismatched(self) -> list[MemberCheck]:
        return [c for c in self.checks if c.status == "mismatch"]

    @property
    def missing(self) -> list[MemberCheck]:
        return [c for c in self.checks if c.status == "missing"]
