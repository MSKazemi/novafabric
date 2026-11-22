"""Hugging Face Hub revision references with an explicit evidence strength (HF-03, experimental).

A capsule that used a Hub model or dataset can carry a *receipt* saying which repository
revision it meant, whether the Hub confirmed the revision, and whether bytes held locally
match a digest the Hub published. The receipt lives in the manifest's vendor ``extensions``
under :data:`EXTENSION_KEY`. It is additive and optional: a capsule without the key is valid
and unchanged, and no new top-level format is introduced.

Three strengths, deliberately kept apart (:data:`EvidenceStrength`):

* ``declared_only`` -- somebody *stated* ``repo_id`` and a revision. Nothing was checked.
* ``hub_resolved`` -- the Hub, asked anonymously, reported an immutable commit SHA for the
  requested revision at ``resolved_at``. That is the Hub's claim about the *repository*
  revision. It is **not** proof of the weights a provider served, and it says nothing about
  files nobody hashed. A branch or tag name resolves to a point-in-time SHA that can move.
* ``local_bytes_verified`` -- at least one file's bytes on this machine hash to the SHA-256
  the Hub published (Git LFS) for that revision. The claim covers exactly those files.

What this module never does: touch the network on import, in :func:`declare_reference` or in
:func:`verify_local_file`; download a file; require a token (resolution is anonymous unless
the caller passes one); or copy free text from the Hub or from an exception into the
receipt -- every Hub-supplied value is matched against a strict pattern first and reasons
are a closed vocabulary, so a hostile or compromised response cannot inject content and an
exception message that echoes a credential cannot reach a capsule.

``nova verify`` does not use this module: offline verification never needs the Hub.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

EXTENSION_KEY = "dev.novafabric.huggingface-hub"
SCHEMA_VERSION = "0.1.0"

RepoType = Literal["model", "dataset", "space"]
EvidenceStrength = Literal["declared_only", "hub_resolved", "local_bytes_verified"]
ResolutionStatus = Literal["resolved", "declared_only", "unavailable", "error"]
#: Closed vocabulary: the only explanations a receipt can carry.
Reason = Literal[
    "offline",
    "dependency_missing",
    "unauthorized_or_gated",
    "not_found",
    "revision_not_found",
    "rate_limited",
    "hub_unreachable",
    "hub_error",
    "invalid_metadata",
    "pinned_revision_mismatch",
    "local_digest_mismatch",
    "local_file_unreadable",
]
DriftVerdict = Literal["same", "moved", "unknown"]

#: What each strength does and does not mean, stamped into every receipt so a reader of the
#: raw JSON sees the limit without having to find this module.
CLAIMS: dict[str, str] = {
    "declared_only": "Stated by the caller; nothing was checked.",
    "hub_resolved": (
        "The Hub reported this commit for the requested revision at resolved_at. A Hub commit "
        "is not proof of the weights a provider served, and a branch or tag can move."
    ),
    "local_bytes_verified": (
        "Local file bytes hash to the SHA-256 the Hub published for this revision; the claim "
        "covers only files marked local_verified whose digest source is hub_lfs. A declared "
        "digest is the caller's, not the Hub's."
    ),
}

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
_REPO_ID_RE = re.compile(rf"^(?!.*\.\.){_NAME}(/{_NAME})?\Z")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
#: Revisions are branch/tag/commit names. Control characters and spaces are never legitimate.
_REVISION_RE = re.compile(r"^[^\x00-\x20\x7f\\]{1,255}\Z")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_PATH_RE = re.compile(r"^(?!/)(?!.*(^|/)\.\.(/|\Z))[^\x00-\x1f\x7f\\]{1,512}\Z")
_TRUTHY = {"1", "ON", "YES", "TRUE"}

#: Hashing reads in fixed chunks so a multi-gigabyte weight file never sits in memory.
_CHUNK = 1 << 20


class InvalidHubReferenceError(ValueError):
    """A reference was malformed. Raised for *caller input*, never for Hub conditions."""


def _check(pattern: re.Pattern[str], value: object, field: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise InvalidHubReferenceError(f"invalid {field}")
    return value


def _invalid_from(exc: ValidationError) -> InvalidHubReferenceError:
    """Re-raise pydantic's wrapper as the module's own error type, with no input echoed."""
    return InvalidHubReferenceError("; ".join(sorted({e["msg"] for e in exc.errors()})))


class HubFileDigest(BaseModel):
    """One file's digest, and where that digest came from."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    path: str
    sha256: str
    #: ``hub_lfs``: published by the Hub for this revision. ``declared``: supplied by the
    #: caller -- never independent evidence by itself.
    source: Literal["hub_lfs", "declared"]
    size_bytes: int | None = Field(default=None, ge=0)
    #: ``None`` = not checked locally; ``True``/``False`` = the local bytes matched / did not.
    local_verified: bool | None = None

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, v: object) -> str:
        return _check(_PATH_RE, v, "file path")

    @field_validator("sha256", mode="before")
    @classmethod
    def _sha(cls, v: object) -> str:
        return _check(_SHA256_RE, v, "sha256")


class HubReference(BaseModel):
    """A Hub repository revision, with how much of it is actually established."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: str = SCHEMA_VERSION
    repo_type: RepoType
    repo_id: str
    requested_revision: str | None = None
    config: str | None = None
    split: str | None = None
    resolution_status: ResolutionStatus = "declared_only"
    reason: Reason | None = None
    resolved_sha: str | None = None
    #: True only when the *requested* revision was already a full commit SHA. A branch or
    #: tag resolves to a SHA that was current at ``resolved_at`` and may move later.
    pinned: bool = False
    #: Set only when the Hub reports a different (renamed/redirected) repo id.
    canonical_repo_id: str | None = None
    resolved_at: str | None = None
    hub_last_modified: str | None = None
    files: list[HubFileDigest] = Field(default_factory=list)
    #: Requested files the Hub gave no SHA-256 for (not LFS, or not in the revision).
    unresolved_files: list[str] = Field(default_factory=list)
    evidence_strength: EvidenceStrength = "declared_only"
    claim: str = CLAIMS["declared_only"]

    @field_validator("repo_id", "canonical_repo_id", mode="before")
    @classmethod
    def _repo(cls, v: object) -> str | None:
        return None if v is None else _check(_REPO_ID_RE, v, "repo_id")

    @field_validator("requested_revision", mode="before")
    @classmethod
    def _rev(cls, v: object) -> str | None:
        return None if v is None else _check(_REVISION_RE, v, "revision")

    @field_validator("config", "split", mode="before")
    @classmethod
    def _label(cls, v: object) -> str | None:
        return None if v is None else _check(_LABEL_RE, v, "config/split label")

    @field_validator("resolved_sha", mode="before")
    @classmethod
    def _sha(cls, v: object) -> str | None:
        return None if v is None else _check(_COMMIT_RE, v, "resolved_sha")

    @field_validator("unresolved_files", mode="before")
    @classmethod
    def _unresolved(cls, v: object) -> object:
        if isinstance(v, list):
            for item in v:
                _check(_PATH_RE, item, "file path")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> HubReference:
        # A receipt may not claim more than it holds. Everything that raises the strength is
        # tied to a completed resolution, because ``model_copy`` and the manifest read path
        # would otherwise let a hand-made receipt claim evidence nobody gathered.
        resolved = self.resolution_status == "resolved"
        if self.pinned != bool(
            self.requested_revision and _COMMIT_RE.match(self.requested_revision)
        ):
            raise InvalidHubReferenceError("pinned must mirror a full-commit requested_revision")
        if self.evidence_strength != "declared_only" and (
            not resolved or self.resolved_sha is None or self.resolved_at is None
        ):
            raise InvalidHubReferenceError("a resolved strength needs a completed resolution")
        if self.pinned and self.resolved_sha not in (None, self.requested_revision):
            raise InvalidHubReferenceError("resolved_sha differs from the pinned commit")
        if any(f.source == "hub_lfs" for f in self.files) and not resolved:
            raise InvalidHubReferenceError("a Hub digest needs a completed resolution")
        if self.evidence_strength == "local_bytes_verified" and (
            not any(f.local_verified and f.source == "hub_lfs" for f in self.files)
            or any(f.local_verified is False for f in self.files)
        ):
            raise InvalidHubReferenceError("local_bytes_verified needs only verified Hub digests")
        if self.claim != CLAIMS[self.evidence_strength]:
            raise InvalidHubReferenceError("claim text does not match evidence_strength")
        return self


def declare_reference(
    repo_type: str,
    repo_id: str,
    *,
    revision: str | None = None,
    config: str | None = None,
    split: str | None = None,
) -> HubReference:
    """Record what the caller says it used. No network, no validation against the Hub."""
    if repo_type not in ("model", "dataset", "space"):
        raise InvalidHubReferenceError("invalid repo_type")
    try:
        return HubReference(
            repo_type=repo_type,  # type: ignore[arg-type]  # narrowed by the check above
            repo_id=repo_id,
            requested_revision=revision,
            config=config,
            split=split,
            pinned=bool(revision and _COMMIT_RE.match(revision)),
        )
    except ValidationError as exc:
        raise _invalid_from(exc) from None


def _offline(env: dict[str, str] | os._Environ[str]) -> bool:
    # huggingface_hub itself treats TRANSFORMERS_OFFLINE as an alias of HF_HUB_OFFLINE.
    return any(
        env.get(k, "").upper() in _TRUTHY for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    )


def _with(ref: HubReference, **update: Any) -> HubReference:
    """``model_copy`` that re-runs the validators, so a derived receipt is checked too."""
    try:
        return HubReference.model_validate({**ref.model_dump(), **update})
    except ValidationError as exc:
        raise _invalid_from(exc) from None


def _utc(dt: object) -> str | None:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify(exc: BaseException) -> tuple[ResolutionStatus, Reason]:
    """Map a Hub/transport exception to a closed reason. The message is never read."""
    names = {c.__name__ for c in type(exc).__mro__}
    status = getattr(getattr(exc, "response", None), "status_code", None)
    # GatedRepoError subclasses RepositoryNotFoundError, so it is tested first.
    if "GatedRepoError" in names or status in (401, 403):
        return "unavailable", "unauthorized_or_gated"
    if "RevisionNotFoundError" in names:
        return "unavailable", "revision_not_found"
    if "RepositoryNotFoundError" in names or status == 404:
        return "unavailable", "not_found"
    if status == 429:
        return "unavailable", "rate_limited"
    if "OfflineModeIsEnabled" in names:
        return "unavailable", "offline"
    if isinstance(status, int) and status >= 500:
        return "unavailable", "hub_error"
    transport = {"TransportError", "RequestError", "TimeoutException", "NetworkError"}
    if status is None and (isinstance(exc, (OSError, TimeoutError)) or names & transport):
        return "unavailable", "hub_unreachable"
    return "error", "hub_error"


def _lfs_digest(sibling: object) -> tuple[str, int | None] | None:
    lfs = getattr(sibling, "lfs", None)
    if lfs is None:
        return None
    sha = lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)
    size = lfs.get("size") if isinstance(lfs, dict) else getattr(lfs, "size", None)
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        return None
    return sha, size if isinstance(size, int) and size >= 0 else None


def resolve_reference(
    ref: HubReference,
    *,
    files: Sequence[str] = (),
    token: str | None = None,
    api: Any = None,
    timeout: float = 10.0,
    clock: Callable[[], str] = _now,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> HubReference:
    """Ask the Hub which commit *ref* names. Returns a new receipt; never raises for the Hub.

    Anonymous by default (``token=False`` is passed to the client, so an ambient ``HF_TOKEN``
    is *not* sent); pass ``token`` to reach a private or gated repo. Offline mode
    (``HF_HUB_OFFLINE``) returns ``unavailable/offline`` without importing the client.
    *files* names files whose Git-LFS SHA-256 should be recorded (a metadata call, no download).
    *api* is an injection seam: any object with ``model_info``/``dataset_info``/``space_info``.
    """
    env = os.environ if environ is None else environ
    checked_files = [_check(_PATH_RE, f, "file path") for f in files]

    def _fail(status: ResolutionStatus, reason: Reason) -> HubReference:
        # A failed attempt carries no evidence: it must not inherit the previous receipt's
        # SHA or strength, or drift would read a dead Hub as "same".
        return _with(
            ref,
            resolution_status=status,
            reason=reason,
            resolved_sha=None,
            canonical_repo_id=None,
            resolved_at=None,
            hub_last_modified=None,
            files=[],
            unresolved_files=[],
            evidence_strength="declared_only",
            claim=CLAIMS["declared_only"],
        )

    if _offline(env):
        return _fail("unavailable", "offline")
    if api is None:
        try:
            from huggingface_hub import HfApi  # type: ignore[import-not-found,unused-ignore]

            api = HfApi()
        except ImportError:
            return _fail("unavailable", "dependency_missing")
        except Exception:  # noqa: BLE001 - a broken install must not break the workload
            return _fail("error", "invalid_metadata")
    fetch = getattr(api, f"{ref.repo_type}_info", None)
    if not callable(fetch):
        return _fail("error", "invalid_metadata")

    kwargs: dict[str, Any] = {"timeout": timeout, "token": token if token else False}
    if ref.requested_revision:
        kwargs["revision"] = ref.requested_revision
    if checked_files:
        kwargs["files_metadata"] = True
    try:
        info = fetch(ref.repo_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 - every Hub/transport failure becomes a recorded reason
        return _fail(*_classify(exc))

    sha = getattr(info, "sha", None)
    if not isinstance(sha, str) or not _COMMIT_RE.match(sha):
        return _fail("error", "invalid_metadata")
    if ref.pinned and sha != ref.requested_revision:
        # The caller pinned a commit and the Hub answered with another: report, never adopt.
        return _fail("error", "pinned_revision_mismatch")

    canonical: str | None = None
    hub_id = getattr(info, "id", None)
    if isinstance(hub_id, str) and hub_id != ref.repo_id and _REPO_ID_RE.match(hub_id):
        canonical = hub_id if hub_id.lower() != ref.repo_id.lower() else None

    digests: list[HubFileDigest] = []
    unresolved: list[str] = []
    siblings = {getattr(s, "rfilename", None): s for s in (getattr(info, "siblings", None) or [])}
    for path in checked_files:
        found = _lfs_digest(siblings[path]) if path in siblings else None
        if found is None:
            unresolved.append(path)
        else:
            digests.append(
                HubFileDigest(path=path, sha256=found[0], source="hub_lfs", size_bytes=found[1])
            )

    return _with(
        ref,
        resolution_status="resolved",
        reason=None,
        resolved_sha=sha,
        canonical_repo_id=canonical,
        resolved_at=clock(),
        hub_last_modified=_utc(getattr(info, "last_modified", None)),
        files=digests,
        unresolved_files=unresolved,
        evidence_strength="hub_resolved",
        claim=CLAIMS["hub_resolved"],
    )


def _hash_regular_file(path: Path) -> str | None:
    """SHA-256 of a regular file, or ``None``. A link, FIFO or device is refused, not opened.

    The type check is made on the opened descriptor (``O_NOFOLLOW`` + ``O_NONBLOCK`` + ``fstat``),
    so swapping the file for a link or FIFO between check and open cannot be followed or hang.
    Only the final path component is checked: a symlinked *parent* directory is still followed.
    """
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # now owned by the file object
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def verify_local_file(
    ref: HubReference,
    path: str,
    local_path: Path,
    *,
    expected_sha256: str | None = None,
) -> HubReference:
    """Hash *local_path* and compare it with the digest recorded for *path*.

    Uses the Hub-published digest from ``ref.files`` when present. ``expected_sha256`` is for a
    file the Hub gave no digest for: it is recorded with ``source="declared"`` and can never
    raise the strength, because a digest the caller supplied proves nothing about the Hub.
    A mismatch is recorded (``local_digest_mismatch``), never raised and never "fixed".
    """
    _check(_PATH_RE, path, "file path")
    known = next((f for f in ref.files if f.path == path), None)
    if known is None:
        if expected_sha256 is None:
            raise InvalidHubReferenceError("no digest recorded for this path; pass expected_sha256")
        known = HubFileDigest(path=path, sha256=expected_sha256, source="declared")
    actual = _hash_regular_file(local_path)
    ok = actual is not None and actual == known.sha256
    reason: Reason | None = None
    if actual is None:
        reason = "local_file_unreadable"
    elif not ok:
        reason = "local_digest_mismatch"
    checked = known.model_copy(update={"local_verified": ok})
    files = sorted([f for f in ref.files if f.path != path] + [checked], key=lambda f: f.path)
    return _rescore(ref, reason, files)


_LOCAL_REASONS = ("local_digest_mismatch", "local_file_unreadable")


def _rescore(ref: HubReference, reason: Reason | None, files: list[HubFileDigest]) -> HubReference:
    """Raise to ``local_bytes_verified`` only on a Hub-published digest that matched, and fall
    back to the resolution-only strength if any recorded file failed to match.

    ``reason`` is this check's outcome; a stale local-failure reason from an earlier check is
    cleared once no file is failing, and a non-local reason (e.g. ``offline``) is left alone.
    """
    base: EvidenceStrength = "hub_resolved" if ref.resolved_sha else "declared_only"
    verified = any(f.local_verified and f.source == "hub_lfs" for f in files)
    failed = any(f.local_verified is False for f in files)
    strength: EvidenceStrength = "local_bytes_verified" if verified and not failed else base
    if reason is not None:
        new_reason: Reason | None = reason
    elif failed:
        new_reason = ref.reason
    else:
        new_reason = None if ref.reason in _LOCAL_REASONS else ref.reason
    return _with(
        ref, files=files, evidence_strength=strength, claim=CLAIMS[strength], reason=new_reason
    )


def check_revision_drift(previous: HubReference, current: HubReference) -> DriftVerdict:
    """Did the commit a revision name change between two resolutions of the same reference?

    ``unknown`` unless both are resolved and name the same repo and requested revision: an
    unavailable resolution is never read as "same".
    """
    if previous.resolved_sha is None or current.resolved_sha is None:
        return "unknown"
    same_target = (previous.repo_type, previous.repo_id.lower(), previous.requested_revision) == (
        current.repo_type,
        current.repo_id.lower(),
        current.requested_revision,
    )
    if not same_target:
        return "unknown"
    return "same" if previous.resolved_sha == current.resolved_sha else "moved"


def _sort_key(r: HubReference) -> tuple[str, ...]:
    return (r.repo_type, r.repo_id, r.requested_revision or "", r.config or "", r.split or "")


def attach_hub_references(manifest: dict[str, Any], refs: Iterable[HubReference]) -> dict[str, Any]:
    """Return a copy of *manifest* with the receipts under ``extensions[EXTENSION_KEY]``.

    No references means no key: a capsule with nothing to say is byte-identical to before.
    """
    ordered = sorted(refs, key=_sort_key)
    if not ordered:
        return manifest
    out = dict(manifest)
    extensions = dict(out.get("extensions") or {})
    extensions[EXTENSION_KEY] = {
        "schema_version": SCHEMA_VERSION,
        "references": [r.model_dump(exclude_none=True) for r in ordered],
    }
    out["extensions"] = extensions
    return out


def references_from_manifest(manifest: dict[str, Any]) -> list[HubReference]:
    """Read the receipts back. Absent key -> ``[]``; a malformed entry raises (fail closed)."""
    extensions = manifest.get("extensions") if isinstance(manifest, dict) else None
    if extensions is not None and not isinstance(extensions, dict):
        raise InvalidHubReferenceError("malformed manifest extensions")
    block = (extensions or {}).get(EXTENSION_KEY)
    if block is None:
        return []
    if not isinstance(block, dict) or not isinstance(block.get("references"), list):
        raise InvalidHubReferenceError("malformed huggingface-hub extension")
    try:
        return [HubReference.model_validate(item) for item in block["references"]]
    except ValidationError as exc:
        raise _invalid_from(exc) from None
