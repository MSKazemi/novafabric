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
"""The one legal-hold store (ADR-0031 §3/§4, ADR-0134 D4, ADR-0206 D2).

Every writer and every deletion reader goes through this module:

* **writers** — ``nova hold create|release`` and ``POST /api/holds[/{id}/release]``;
* **readers** — ``nova retention plan|apply|status|explain``, ``nova capsule delete``,
  ``nova export-retention``, the governed server/serve delete paths
  (:func:`novafabric.server.capsule_delete.active_hold_ids`, which also backs
  ``metadata_store.run_delete`` and the evidence cart), and ``GET /api/holds``.

**Where holds live.** New holds are written to exactly one place,
:func:`novafabric._paths.legal_holds_root` — ``$NOVAFABRIC_HOME/registries/<registry>/
holds.jsonl``. Before this module the CLI wrote ``./.novafabric/registries`` under the
shell's working directory and the API wrote ``<capsule_dir>/../registries``, and each
deletion path read only one of them, so a hold placed on one surface did not stop
deletion on the other.

**Holds already on disk stay honoured.** Readers consult, in order and de-duplicated:
the canonical root, the legacy CLI root (``./.novafabric/registries`` under the
current working directory) and any root the caller knows about (the serve/server
capsule base's ``<capsule_dir>/../registries``). ``release`` finds a hold in any of
them. Nothing is moved: a hold file is legal evidence and is never rewritten except to
stamp ``released_at``.

**Fail closed.** A ledger line that cannot be parsed, or a ledger that cannot be read,
is reported as an active hold with id ``__corrupt__:<registry>`` — "we could not read
it" never reads as "there is no hold".

Record format (one JSON object per line, unchanged)::

    {"hold_id": str, "registry": str, "reason": str, "duration_days": int | null,
     "created_at": str, "released_at": str | null}
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric import _paths
from novafabric._safe_path import atomic_write_bytes

logger = logging.getLogger(__name__)

HOLDS_FILENAME = "holds.jsonl"
_LOCK_FILENAME = ".holds.lock"
CORRUPT_HOLD_PREFIX = "__corrupt__:"
_MAX_REGISTRY_LEN = 255


class HoldStoreError(Exception):
    """Base class for legal-hold store failures."""


class InvalidRegistryNameError(HoldStoreError, ValueError):
    """The registry name is empty or could escape the hold store directory."""


class HoldNotFoundError(HoldStoreError, LookupError):
    """No active hold with this id exists in any hold location."""


@dataclass(frozen=True)
class HoldRecord:
    """One legal hold as read from a ledger, plus the file it came from."""

    hold_id: str
    registry: str
    reason: str
    duration_days: int | None
    created_at: str
    released_at: str | None
    source: Path

    @property
    def active(self) -> bool:
        return self.released_at is None

    @property
    def corrupt(self) -> bool:
        return self.hold_id.startswith(CORRUPT_HOLD_PREFIX)

    def as_dict(self) -> dict[str, Any]:
        """The stored/API shape (``source`` is local bookkeeping, not part of it)."""
        return {
            "hold_id": self.hold_id,
            "registry": self.registry,
            "reason": self.reason,
            "duration_days": self.duration_days,
            "created_at": self.created_at,
            "released_at": self.released_at,
        }


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def canonical_holds_root() -> Path:
    """Where every new hold is written (``$NOVAFABRIC_HOME/registries``)."""
    return _paths.legal_holds_root()


def legacy_cli_holds_root() -> Path:
    """Where ``nova hold`` wrote before unification: ``./.novafabric/registries``."""
    return Path.cwd() / ".novafabric" / "registries"


def capsule_base_holds_root(capsule_dir: Path) -> Path:
    """Where ``POST /api/holds`` wrote before unification: ``<capsule_dir>/../registries``."""
    return capsule_dir.parent / "registries"


def hold_roots(extra_roots: Iterable[Path] = ()) -> list[Path]:
    """Every location a hold may live in: canonical first, then legacy, de-duplicated."""
    roots: list[Path] = []
    seen: set[Path] = set()
    for root in (canonical_holds_root(), legacy_cli_holds_root(), *extra_roots):
        key = root.resolve()
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


RETENTION_POLICY_FILENAME = "retention-policy.yaml"
WORM_DB_FILENAME = "worm.db"


def canonical_registry_file(registry: str, filename: str) -> Path:
    """Where a per-registry file belongs: ``$NOVAFABRIC_HOME/registries/<r>/<filename>``.

    The path a new file is written to. It is not created here.
    """
    return canonical_holds_root() / validate_registry_name(registry) / filename


def registry_file_paths(
    registry: str, filename: str, *, extra_roots: Iterable[Path] = ()
) -> list[Path]:
    """Every existing ``<root>/<registry>/<filename>``, canonical first.

    Resolved like holds (:func:`hold_roots`): ``$NOVAFABRIC_HOME/registries/<r>/``,
    then the legacy ``./.novafabric/registries/<r>/`` under the working directory,
    then any caller-known root.
    """
    validate_registry_name(registry)
    return [
        path
        for root in hold_roots(extra_roots)
        if (path := root / registry / filename).is_file()
    ]


def warn_legacy_registry_file(path: Path, canonical: Path, *, used: bool) -> None:
    """Log that a per-registry file outside ``$NOVAFABRIC_HOME/registries`` was found.

    *used* says whether the caller reads it (no canonical copy, or a fail-closed
    gate that consults every copy) or ignores it because the canonical copy wins.
    """
    logger.warning(
        "%s: legacy per-registry file %s, %s; move it to %s",
        path.name,
        path,
        "read" if used else "ignored because the canonical copy takes precedence",
        canonical,
        extra={"legacy_path": str(path), "canonical_path": str(canonical), "used": used},
    )


def preferred_registry_file(registry: str, filename: str) -> Path:
    """The one copy of a per-registry file to read, or where a new one belongs.

    The canonical copy when it exists; otherwise the legacy working-directory copy
    when that exists (logged as a warning); otherwise the canonical path. A legacy
    copy shadowed by a canonical one is logged too, so a stale file is never
    silently ignored. Fail-closed gates that must honour *every* copy use
    :func:`registry_file_paths` instead.
    """
    canonical = canonical_registry_file(registry, filename)
    found = registry_file_paths(registry, filename)
    if not found:
        return canonical
    chosen = found[0]
    for path in found:
        if path != canonical:
            warn_legacy_registry_file(path, canonical, used=path == chosen)
    return chosen


class RegistryStateUnreadableError(HoldStoreError):
    """A per-registry location exists but cannot be inspected (fail closed)."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def entry_present(path: Path) -> bool:
    """True when a directory entry exists at *path*, whatever it is.

    A regular file, a directory, a special file and a dangling symbolic link all
    count: each is configured state that a deletion gate must read, not skip.
    ``Path.exists``/``is_file`` answer ``False`` for a dangling link and, since
    Python 3.14, for any ``OSError`` (a registry directory without search
    permission included), so they cannot tell "absent" from "cannot tell".

    Raises:
        RegistryStateUnreadableError: the entry cannot be examined (for example
            permission denied on the directory holding it).
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise RegistryStateUnreadableError(path, f"cannot be examined: {exc}") from exc
    return True


def registry_file_entries(
    registry: str, filename: str, *, extra_roots: Iterable[Path] = ()
) -> list[Path]:
    """Every ``<root>/<registry>/<filename>`` entry present on disk, canonical first.

    Like :func:`registry_file_paths`, but for fail-closed gates: an entry that is
    not a readable regular file (a dangling link, a directory, an unreadable file)
    is returned too, so that reading it fails and the gate refuses.

    Raises:
        RegistryStateUnreadableError: see :func:`entry_present`.
    """
    validate_registry_name(registry)
    return [
        path
        for root in hold_roots(extra_roots)
        if entry_present(path := root / registry / filename)
    ]


def retention_policy_paths(
    registry: str, *, extra_roots: Iterable[Path] = ()
) -> list[Path]:
    """Every ``retention-policy.yaml`` entry for *registry*, canonical first.

    Resolved by :func:`registry_file_entries`. A deletion gate must apply **every**
    file found, so the stricter one wins and a stale legacy policy is never
    silently ignored; an entry that cannot be read must refuse, not be skipped.

    Raises:
        RegistryStateUnreadableError: see :func:`entry_present`.
    """
    return registry_file_entries(registry, RETENTION_POLICY_FILENAME, extra_roots=extra_roots)


def validate_registry_name(registry: str) -> str:
    """Return *registry* if it names exactly one directory inside a hold root.

    Raises:
        InvalidRegistryNameError: empty/blank, too long, a path separator, NUL, or
            a ``.``/``..`` component.
    """
    if not registry or not registry.strip():
        raise InvalidRegistryNameError("registry must not be empty")
    if (
        len(registry) > _MAX_REGISTRY_LEN
        or registry != registry.strip()
        or any(ch in registry for ch in ("/", "\\", "\x00"))
        or ".." in registry
        or registry == "."
    ):
        raise InvalidRegistryNameError(
            f"invalid registry name {registry!r}: must be one path component "
            "without '/', '\\', '..' or surrounding whitespace"
        )
    return registry


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _corrupt(registry: str, path: Path, why: str) -> HoldRecord:
    logger.warning(
        "legal-hold ledger %s: %s — treated as an active hold (fail closed)", path, why
    )
    return HoldRecord(
        hold_id=f"{CORRUPT_HOLD_PREFIX}{registry}",
        registry=registry,
        reason=f"unreadable legal-hold ledger entry in {path}: {why}",
        duration_days=None,
        created_at="",
        released_at=None,
        source=path,
    )


def _parse_line(line: str, registry: str, path: Path) -> HoldRecord:
    try:
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise TypeError("not a JSON object")
        hold_id = raw["hold_id"]
        if not isinstance(hold_id, str) or not hold_id:
            raise TypeError("hold_id is not a non-empty string")
        duration = raw.get("duration_days")
        released = raw.get("released_at")
        return HoldRecord(
            hold_id=hold_id,
            registry=registry,
            reason=str(raw.get("reason", "")),
            duration_days=duration if isinstance(duration, int) else None,
            created_at=str(raw.get("created_at", "")),
            released_at=None if released is None else str(released),
            source=path,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return _corrupt(registry, path, f"{type(exc).__name__}: {exc}")


def read_ledger(path: Path, registry: str) -> list[HoldRecord]:
    """Every record in one ``holds.jsonl`` (active and released), fail-closed on damage."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [_corrupt(registry, path, f"unreadable: {exc}")]
    return [_parse_line(line, registry, path) for line in text.splitlines() if line.strip()]


def _readable_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _ledger_entry(path: Path) -> bool:
    """True unless *path* is certainly absent; "cannot tell" counts as present.

    ``Path.is_file`` raises ``PermissionError`` on Python 3.12 and answers ``False``
    on 3.14 when the registry directory cannot be searched, so it either crashes
    the gate or hides the ledger. :func:`read_ledger` turns an entry it cannot
    read into an active hold, which is the fail-closed answer on both.
    """
    try:
        return entry_present(path)
    except RegistryStateUnreadableError:
        return True


def _ledgers(
    registry: str | None, extra_roots: Iterable[Path]
) -> Iterator[tuple[str, Path, str | None]]:
    """``(registry, ledger, error)`` for every hold ledger; *error* if unlistable."""
    for root in hold_roots(extra_roots):
        if registry is not None:
            path = root / registry / HOLDS_FILENAME
            if _ledger_entry(path):
                yield registry, path, None
            continue
        try:
            if not root.is_dir():
                continue
            reg_dirs = sorted(root.iterdir())
        except OSError as exc:
            yield "*", root, f"hold location cannot be listed: {exc}"
            continue
        for reg_dir in reg_dirs:
            path = reg_dir / HOLDS_FILENAME
            if reg_dir.is_dir() and _ledger_entry(path):
                yield reg_dir.name, path, None


def read_holds(
    registry: str | None = None, *, extra_roots: Iterable[Path] = ()
) -> list[HoldRecord]:
    """All hold records (active and released) across every hold location.

    *registry* ``None`` means every registry. A hold id seen in several files is
    reported once, and as active if **any** copy is still active.
    """
    if registry is not None:
        validate_registry_name(registry)
    merged: dict[tuple[str, str], HoldRecord] = {}
    for reg, path, error in _ledgers(registry, list(extra_roots)):
        records = [_corrupt(reg, path, error)] if error else read_ledger(path, reg)
        for record in records:
            key = (reg, record.hold_id)
            previous = merged.get(key)
            if previous is None or (record.active and not previous.active):
                merged[key] = record
    return list(merged.values())


def active_holds(
    registry: str | None = None, *, extra_roots: Iterable[Path] = ()
) -> list[HoldRecord]:
    """Unreleased holds (corrupt entries included, fail closed)."""
    return [h for h in read_holds(registry, extra_roots=extra_roots) if h.active]


def active_hold_ids(
    registry: str | None = None, *, extra_roots: Iterable[Path] = ()
) -> list[str]:
    """Ids of :func:`active_holds` — what every deletion gate consults."""
    return [h.hold_id for h in active_holds(registry, extra_roots=extra_roots)]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _ledger_lock(reg_dir: Path) -> Iterator[None]:
    """Serialize appends and release-rewrites of one ledger (POSIX advisory lock).

    Without it a release rewrite racing an append could drop the newly placed
    hold. On platforms without ``fcntl`` the lock is a no-op.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return
    reg_dir.mkdir(parents=True, exist_ok=True)
    with (reg_dir / _LOCK_FILENAME).open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _audit(event: str, *, actor: str, registry: str, details: dict[str, Any]) -> None:
    from novafabric.audit import AuditEventType, AuditLog, resolve_audit_log_path

    AuditLog(resolve_audit_log_path()).append(
        event_type=AuditEventType(event),
        actor=actor,
        resource_id=registry,
        details=details,
    )


def place_hold(
    registry: str,
    reason: str,
    duration_days: int | None = None,
    *,
    actor: str,
) -> HoldRecord:
    """Append a new active hold to the canonical ledger and audit it (``hold.create``).

    Raises:
        InvalidRegistryNameError: *registry* is not a single safe path component.
        ValueError: *reason* is blank or *duration_days* is negative.
    """
    validate_registry_name(registry)
    if not reason.strip():
        raise ValueError("reason must not be empty")
    if duration_days is not None and duration_days < 0:
        raise ValueError("duration_days must be >= 0")
    record = HoldRecord(
        hold_id=f"hold-{uuid.uuid4().hex[:8]}",
        registry=registry,
        reason=reason,
        duration_days=duration_days,
        created_at=datetime.now(tz=timezone.utc).isoformat(),
        released_at=None,
        source=canonical_holds_root() / registry / HOLDS_FILENAME,
    )
    reg_dir = record.source.parent
    with _ledger_lock(reg_dir):
        with record.source.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.as_dict()) + "\n")
            f.flush()
            os.fsync(f.fileno())
    _audit(
        "hold.create",
        actor=actor,
        registry=registry,
        details={"hold_id": record.hold_id, "reason": reason, "duration_days": duration_days},
    )
    return record


def _release_in(path: Path, hold_id: str, released_at: str) -> bool:
    """Stamp ``released_at`` on every active copy of *hold_id* in one ledger."""
    with _ledger_lock(path.parent):
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        changed = False
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                out.append(line)  # kept verbatim: still reads as a (corrupt) hold
                continue
            if (
                isinstance(raw, dict)
                and raw.get("hold_id") == hold_id
                and raw.get("released_at") is None
            ):
                raw["released_at"] = released_at
                changed = True
                out.append(json.dumps(raw))
            else:
                out.append(line)
        if changed:
            # Durable (fsync, rename, fsync dir): a crash must never leave an
            # empty ledger, which would read as "no holds" (X2, fail open).
            atomic_write_bytes(
                path,
                ("\n".join(out) + "\n").encode("utf-8"),
                mode=stat.S_IMODE(os.lstat(path).st_mode),
            )
    return changed


def release_hold(
    hold_id: str, *, actor: str, extra_roots: Iterable[Path] = ()
) -> list[str]:
    """Release *hold_id* wherever it is active; return the registries it was released in.

    Searches the canonical root and every legacy root, so a hold placed before
    unification (by either surface) can be released from either surface.

    Raises:
        InvalidRegistryNameError: *hold_id* contains a path separator or ``..``.
        HoldNotFoundError: no active hold with this id exists anywhere.
    """
    if not hold_id or any(ch in hold_id for ch in ("/", "\\", "\x00")) or ".." in hold_id:
        raise InvalidRegistryNameError(f"invalid hold id {hold_id!r}")
    released_at = datetime.now(tz=timezone.utc).isoformat()
    registries: list[str] = []
    for registry, path, error in _ledgers(None, list(extra_roots)):
        # Only a readable ledger can carry a release; an unreadable one stays a hold.
        if error or not _readable_file(path):
            continue
        if _release_in(path, hold_id, released_at) and registry not in registries:
            registries.append(registry)
    if not registries:
        raise HoldNotFoundError(f"Hold not found or already released: {hold_id}")
    for registry in registries:
        _audit("hold.release", actor=actor, registry=registry, details={"hold_id": hold_id})
    return registries
