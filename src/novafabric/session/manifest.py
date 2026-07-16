"""The ``SessionManifest`` record and its local-first storage (ADR-0122).

A session lives entirely on local disk:
``$NOVAFABRIC_HOME/sessions/<session_id>/session.json`` (override the root
with ``NOVAFABRIC_SESSION_DIR``). The manifest is the **authoritative**
ordered index of the session's member runs; each member is referenced by a
content-addressed ``capsule_ref`` (``[<relative-path>@]sha256:<hex>`` over
the member's ``capsule.yaml`` bytes) and is never copied.

Schema: ``schemas/session-manifest.schema.json`` (v0.1.0), graduated from
the accepted design draft (``design/spec/session-capsule-v0.md``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from novafabric._paths import nova_home
from novafabric.capture._ulid import new_ulid

logger = logging.getLogger(__name__)

SESSION_MANIFEST_FILENAME = "session.json"
SESSION_SCHEMA_VERSION = "0.1.0"

_ULID_PATTERN = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_CAPSULE_REF_PATTERN = re.compile(r"^(?:(?P<path>.+)@)?sha256:(?P<digest>[0-9a-f]{64})$")

SessionKind = Literal["conversation", "workflow", "custom"]


class SessionError(Exception):
    """Base class for session-manifest errors (ADR-0122)."""


class SessionNotFoundError(SessionError):
    """No ``session.json`` exists for the requested session id."""


class SessionIntegrityError(SessionError):
    """The manifest violates a session invariant (ordering, uniqueness, ids)."""


class SessionFinalizedError(SessionError):
    """The session is finalized; adding members requires ``reopen=True``."""


class DuplicateMemberError(SessionError):
    """The capsule's ``run_id`` is already a member of this session."""


class NotACapsuleError(SessionError):
    """The given path is not a readable Run Capsule directory."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class MemberRun(BaseModel):
    """One ordered member reference — a reference to a capsule, never a copy."""

    run_id: str
    capsule_ref: str
    sequence: int = Field(ge=0)
    started_at: str
    role: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "capsule_ref": self.capsule_ref,
            "sequence": self.sequence,
            "started_at": self.started_at,
        }
        if self.role is not None:
            data["role"] = self.role
        return data


class SessionManifest(BaseModel):
    """``session.json`` — ordered, by-reference grouping of N independent runs."""

    schema_version: str = SESSION_SCHEMA_VERSION
    session_id: str
    session_kind: SessionKind
    created_at: str
    member_runs: list[MemberRun] = Field(default_factory=list)
    user_ref: str | None = None
    metadata: dict[str, str] | None = None
    finalized_at: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "session_kind": self.session_kind,
            "created_at": self.created_at,
            "member_runs": [m.to_json_dict() for m in self.member_runs],
        }
        if self.user_ref is not None:
            data["user_ref"] = self.user_ref
        if self.metadata is not None:
            data["metadata"] = self.metadata
        if self.finalized_at is not None:
            data["finalized_at"] = self.finalized_at
        return data


def validate_ordering(manifest: SessionManifest) -> None:
    """Enforce the cross-item invariants JSON Schema cannot express.

    ``sequence`` values MUST be unique and ``member_runs`` sorted ascending
    (session-capsule-v0 §Edge cases).

    Raises:
        SessionIntegrityError: On a duplicate or out-of-order ``sequence``.
    """
    sequences = [m.sequence for m in manifest.member_runs]
    if len(set(sequences)) != len(sequences):
        raise SessionIntegrityError(
            f"session {manifest.session_id}: duplicate sequence values "
            f"{sorted(s for s in sequences if sequences.count(s) > 1)}"
        )
    if sequences != sorted(sequences):
        raise SessionIntegrityError(
            f"session {manifest.session_id}: member_runs are not sorted by "
            f"ascending sequence ({sequences})"
        )


def sessions_root(root: Path | None = None) -> Path:
    """Root directory holding session manifests.

    Override with ``NOVAFABRIC_SESSION_DIR``; falls back to
    ``$NOVAFABRIC_HOME/sessions``.
    """
    if root is not None:
        return root
    env = os.environ.get("NOVAFABRIC_SESSION_DIR")
    return Path(env) if env else nova_home() / "sessions"


def session_manifest_path(session_id: str, root: Path | None = None) -> Path:
    """``<sessions-root>/<session_id>/session.json``."""
    return sessions_root(root) / session_id / SESSION_MANIFEST_FILENAME


def new_session(
    kind: SessionKind = "conversation",
    user_ref: str | None = None,
    metadata: dict[str, str] | None = None,
) -> SessionManifest:
    """Create an empty session manifest with a fresh time-prefixed ULID."""
    return SessionManifest(
        session_id=new_ulid(),
        session_kind=kind,
        created_at=_now(),
        user_ref=user_ref,
        metadata=metadata,
    )


def save_session(manifest: SessionManifest, root: Path | None = None) -> Path:
    """Write ``session.json`` (after re-checking ordering); returns its path."""
    validate_ordering(manifest)
    path = session_manifest_path(manifest.session_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_json_dict(), indent=2) + "\n", encoding="utf-8"
    )
    return path


def load_session(session_id: str, root: Path | None = None) -> SessionManifest:
    """Load and integrity-check one session manifest.

    Raises:
        SessionNotFoundError: No manifest exists for *session_id*.
        SessionIntegrityError: The manifest is malformed or breaks ordering.
    """
    path = session_manifest_path(session_id, root)
    if not path.is_file():
        raise SessionNotFoundError(
            f"unknown session {session_id!r}: no {SESSION_MANIFEST_FILENAME} "
            f"under {path.parent.parent}"
        )
    return _load_manifest_file(path)


def _load_manifest_file(path: Path) -> SessionManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = SessionManifest.model_validate(raw)
    except (ValueError, OSError) as exc:  # json, pydantic, io
        raise SessionIntegrityError(f"unreadable session manifest {path}: {exc}") from exc
    validate_ordering(manifest)
    return manifest


def list_sessions(root: Path | None = None) -> list[SessionManifest]:
    """Enumerate sessions by scanning the sessions root (newest first).

    A directory scan, not the ADR-0122 P3 SQLite index (still future design);
    ULIDs are time-prefixed so lexicographic order is creation order.
    Unreadable manifests are warned about and skipped, never fatal.
    """
    base = sessions_root(root)
    if not base.is_dir():
        return []
    manifests: list[SessionManifest] = []
    for entry in sorted(base.iterdir(), reverse=True):
        path = entry / SESSION_MANIFEST_FILENAME
        if not path.is_file():
            continue
        try:
            manifests.append(_load_manifest_file(path))
        except SessionIntegrityError as exc:
            logger.warning("Skipping unreadable session manifest: %s", exc)
    return manifests


def capsule_manifest_digest(capsule_dir: Path) -> str:
    """``sha256:<hex>`` over the member capsule's ``capsule.yaml`` bytes."""
    manifest_file = capsule_dir / "capsule.yaml"
    if not manifest_file.is_file():
        raise NotACapsuleError(f"{capsule_dir} has no capsule.yaml — not a Run Capsule")
    return "sha256:" + hashlib.sha256(manifest_file.read_bytes()).hexdigest()


def parse_capsule_ref(capsule_ref: str) -> tuple[str | None, str]:
    """Split a ``[<path>@]sha256:<hex>`` ref into ``(path_prefix, digest)``."""
    match = _CAPSULE_REF_PATTERN.match(capsule_ref)
    if match is None:
        raise SessionIntegrityError(f"malformed capsule_ref {capsule_ref!r}")
    return match.group("path"), "sha256:" + match.group("digest")


def _relative_ref_path(capsule_dir: Path, session_dir: Path) -> str:
    """Portable path prefix for a capsule ref, relative to the manifest dir."""
    try:
        return os.path.relpath(capsule_dir.resolve(), session_dir.resolve())
    except ValueError:  # pragma: no cover - different drives (Windows only)
        return str(capsule_dir.resolve())


def add_member(
    manifest: SessionManifest,
    capsule_dir: Path,
    root: Path | None = None,
    role: str | None = None,
    reopen: bool = False,
) -> MemberRun:
    """Append *capsule_dir* as the next ordered member (in memory).

    Reads the member capsule (never writes it — one capsule, one writer),
    auto-assigns the next ``sequence``, and records a content-addressed
    ``capsule_ref`` (relative path + sha256 of ``capsule.yaml``). The caller
    persists with :func:`save_session`.

    Raises:
        SessionFinalizedError: The session is finalized and ``reopen`` is False.
        NotACapsuleError: *capsule_dir* is not a readable Run Capsule.
        DuplicateMemberError: The capsule is already a member of this session.
    """
    if manifest.finalized_at is not None and not reopen:
        raise SessionFinalizedError(
            f"session {manifest.session_id} was finalized at "
            f"{manifest.finalized_at}; pass reopen to add more members"
        )

    manifest_file = capsule_dir / "capsule.yaml"
    if not manifest_file.is_file():
        raise NotACapsuleError(f"{capsule_dir} has no capsule.yaml — not a Run Capsule")
    try:
        capsule = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise NotACapsuleError(f"unreadable capsule.yaml in {capsule_dir}: {exc}") from exc
    if not isinstance(capsule, dict) or "run_id" not in capsule:
        raise NotACapsuleError(f"{manifest_file} carries no run_id — not a Run Capsule")

    run_id = str(capsule["run_id"])
    if any(m.run_id == run_id for m in manifest.member_runs):
        raise DuplicateMemberError(
            f"run {run_id} is already a member of session {manifest.session_id} "
            "(a capsule appears at most once per session)"
        )

    # Manifest-wins advisory check: a capsule-side back-reference that points
    # at a DIFFERENT session is recorded verbatim in that capsule; the session
    # manifest being built here stays authoritative (warn, never repair).
    capsule_session_id = capsule.get("session_id")
    if capsule_session_id is not None and capsule_session_id != manifest.session_id:
        logger.warning(
            "capsule %s carries session_id %s but is being added to session %s "
            "— the session manifest is authoritative (capsule fields are advisory)",
            run_id,
            capsule_session_id,
            manifest.session_id,
        )

    session_dir = session_manifest_path(manifest.session_id, root).parent
    digest = capsule_manifest_digest(capsule_dir)
    member = MemberRun(
        run_id=run_id,
        capsule_ref=f"{_relative_ref_path(capsule_dir, session_dir)}@{digest}",
        sequence=(
            max(m.sequence for m in manifest.member_runs) + 1
            if manifest.member_runs
            else 0
        ),
        started_at=str(capsule.get("created_at", _now())),
        role=role,
    )
    capsule_sequence = capsule.get("sequence")
    if (
        capsule_session_id == manifest.session_id
        and capsule_sequence is not None
        and capsule_sequence != member.sequence
    ):
        logger.warning(
            "capsule %s carries sequence %s but session %s assigns sequence %s "
            "— the session manifest is authoritative (capsule fields are advisory)",
            run_id,
            capsule_sequence,
            manifest.session_id,
            member.sequence,
        )
    manifest.member_runs.append(member)
    return member
