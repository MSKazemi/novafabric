"""Declarative backup coverage table (ADR-0216 D1/D2).

Single source of truth for WHAT a ``local-full`` set contains beyond the
original four components, shared by create (collection), restore (origin
mapping), and the CLI (coverage display). Every component always yields a
:class:`~novafabric.backup.models.CoverageEntry` — ``included``, ``absent``,
``skipped`` (with the reason), or ``excluded`` — so what a backup does NOT
contain is part of the signed manifest, never silent.

Origin roots: ``home`` members map 1:1 under the NovaFabric home (0.1.x
behaviour, unchanged); ``audit`` / ``keyring`` / ``seal-config`` members are
archived under ``external/<origin>/`` and restored to their real roots via
:func:`resolve_origin_root`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from novafabric.audit import AUDIT_LOG_PATH
from novafabric.backup.models import BackupMember, CoverageEntry, MemberOrigin


def default_audit_log_path() -> Path:
    """Deployment audit-log path, overridable via ``NOVAFABRIC_AUDIT_LOG_PATH``.

    The audit module hard-codes ``~/.local/share/novafabric/audit.jsonl``; the
    env override exists so backup/restore can be pointed elsewhere (relocated
    deployments) and so the hermetic test fixture keeps tests away from the
    developer's real audit chain.
    """
    env = os.environ.get("NOVAFABRIC_AUDIT_LOG_PATH")
    return Path(env) if env else AUDIT_LOG_PATH

#: Arc-path prefix for members that do not live under the home.
EXTERNAL_PREFIX = "external"

#: SQLite state stores under the home, snapshotted via the online-backup API.
#: name -> (filename, sensitive)
SQLITE_STATE_DBS: tuple[tuple[str, str, bool], ...] = (
    ("incidents", "incidents.db", False),
    ("metadata", "metadata.db", False),
    ("dek", "dek.db", True),
    ("tsa-nonces", "tsa_nonces.db", False),
)

#: Plain-copy components under the home. name -> (relative path, is_dir)
COPY_COMPONENTS: tuple[tuple[str, str, bool], ...] = (
    ("dashboard-audit", "dashboard-audit.jsonl", False),
    ("spool", "spool", True),
)

RATCHET_REL = PurePosixPath("seal/ratchet")
DASHBOARD_DUCKDB = "dashboard.duckdb"
MERKLE_DB = "novaseal-merkle.db"
AUDIT_ARC = f"{EXTERNAL_PREFIX}/audit/audit.jsonl"

#: Every component name a local-full coverage list reports on, in order.
ALL_COMPONENTS: tuple[str, ...] = (
    "registry",
    "capsules",
    "config",
    *(name for name, _, _ in SQLITE_STATE_DBS),
    "seal-merkle",
    "ratchet",
    "dashboard",
    *(name for name, _, _ in COPY_COMPONENTS),
    "audit-log",
    "keys",
)


class CoverageError(Exception):
    """Raised when a component that exists cannot be snapshotted safely."""


@dataclass(frozen=True)
class CollectedComponent:
    """Result of collecting one component into the staging directory."""

    members: list[BackupMember]
    coverage: CoverageEntry


def resolve_origin_root(
    origin: MemberOrigin,
    home: Path,
    *,
    audit_log_path: Optional[Path] = None,
    keyring_dir: Optional[Path] = None,
    seal_config_dir: Optional[Path] = None,
) -> Path:
    """Filesystem root a member origin maps to (create and restore share this)."""
    if origin == "home":
        return home
    if origin == "audit":
        return (audit_log_path or default_audit_log_path()).parent
    if origin == "keyring":
        if keyring_dir is not None:
            return keyring_dir
        from novafabric.trust.keyring import _KEYRING_DIR

        return _KEYRING_DIR
    if origin == "seal-config":
        # novaseal.yaml + its PEMs restore next to the home by default; the
        # operator may need to re-point key_path/cert_path if they lived
        # elsewhere (stated in the restore step detail, never silent).
        return seal_config_dir if seal_config_dir is not None else home
    raise CoverageError(f"Unknown member origin {origin!r}")


def strip_external_prefix(arc_path: str) -> str:
    """``external/<origin>/rest`` -> ``rest`` (home paths pass through)."""
    parts = PurePosixPath(arc_path).parts
    if len(parts) >= 3 and parts[0] == EXTERNAL_PREFIX:
        return str(PurePosixPath(*parts[2:]))
    return arc_path


def sqlite_snapshot(src_db: Path, dst_db: Path) -> None:
    """Consistent SQLite snapshot via the online-backup API (ADR-0181 D5)."""
    src = sqlite3.connect(src_db)
    try:
        dst = sqlite3.connect(dst_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        raise CoverageError(f"SQLite online backup of {src_db} failed: {exc}") from exc
    finally:
        src.close()


def duckdb_snapshot(src_db: Path, dst_db: Path) -> Optional[str]:
    """Consistent DuckDB snapshot via ``COPY FROM DATABASE`` (ADR-0216 D1).

    A DuckDB file is not SQLite and a raw copy of a live-writer file is torn,
    so the snapshot goes through DuckDB itself. Returns None on success, or
    the skip reason when the store is locked by a live writer / the duckdb
    module is unavailable (the store is a derived, rebuildable topology
    cache — skipping loses no evidence).
    """
    try:
        import duckdb
    except ImportError:
        return "duckdb module not installed — derived topology cache skipped"
    try:
        conn = duckdb.connect(":memory:")
        try:
            conn.execute(f"ATTACH '{src_db}' AS src (READ_ONLY)")
            conn.execute(f"ATTACH '{dst_db}' AS dst")
            conn.execute("COPY FROM DATABASE src TO dst")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — any failure means "skip, honestly"
        dst_db.unlink(missing_ok=True)
        return f"dashboard.duckdb not snapshottable ({exc}) — derived cache, rebuilt by nova serve"
    return None


def collect_extended_components(
    home: Path,
    stage: Path,
    member_fn: Callable[..., BackupMember],
    *,
    audit_log_path: Optional[Path] = None,
    include_keys: bool = False,
    keyring_dir: Optional[Path] = None,
    seal_config_path: Optional[Path] = None,
) -> list[CollectedComponent]:
    """Collect every ADR-0216 component into *stage*, with coverage rows.

    ``member_fn`` is ``create._member`` (hash + size), injected to avoid an
    import cycle. Returns one :class:`CollectedComponent` per component in
    table order; callers append the members and coverage rows to the manifest.
    """
    out: list[CollectedComponent] = []

    # --- SQLite state stores -------------------------------------------------
    for name, filename, sensitive in SQLITE_STATE_DBS:
        src = home / filename
        if not src.is_file():
            out.append(_absent(name))
            continue
        dst = stage / filename
        sqlite_snapshot(src, dst)
        out.append(
            CollectedComponent(
                members=[
                    member_fn(
                        dst, filename, kind="state_db", role=name, sensitive=sensitive
                    )
                ],
                coverage=CoverageEntry(component=name, status="included", detail=None),
            )
        )

    # --- NovaSeal Merkle transparency log ------------------------------------
    out.append(_collect_merkle(home, stage, member_fn))

    # --- Ratchet state (forward-secure — sensitive) ---------------------------
    out.append(_collect_ratchet(home, stage, member_fn))

    # --- Dashboard DuckDB (derived cache; DuckDB-native snapshot) -------------
    out.append(_collect_dashboard(home, stage, member_fn))

    # --- Plain-copy components ------------------------------------------------
    for name, rel, is_dir in COPY_COMPONENTS:
        src = home / rel
        if is_dir:
            if not src.is_dir():
                out.append(_absent(name))
                continue
            members = []
            for f in sorted(p for p in src.rglob("*") if p.is_file()):
                arc = PurePosixPath(rel) / f.relative_to(src).as_posix()
                dst = stage / arc
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dst)
                members.append(member_fn(dst, str(arc), kind="log", role=name))
            if not members:
                out.append(_absent(name))
                continue
            out.append(
                CollectedComponent(
                    members=members,
                    coverage=CoverageEntry(component=name, status="included"),
                )
            )
        else:
            if not src.is_file():
                out.append(_absent(name))
                continue
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            out.append(
                CollectedComponent(
                    members=[member_fn(dst, rel, kind="log", role=name)],
                    coverage=CoverageEntry(component=name, status="included"),
                )
            )

    # --- Hash-chained audit log (outside the home) ----------------------------
    log_path = audit_log_path or default_audit_log_path()
    if log_path.is_file():
        dst = stage / AUDIT_ARC
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(log_path, dst)
        out.append(
            CollectedComponent(
                members=[
                    member_fn(dst, AUDIT_ARC, kind="log", role="audit-log", origin="audit")
                ],
                coverage=CoverageEntry(component="audit-log", status="included"),
            )
        )
    else:
        out.append(_absent("audit-log"))

    # --- Key material (ADR-0216 D4: dual opt-in, never by default) ------------
    out.append(
        _collect_keys(
            home,
            stage,
            member_fn,
            include_keys=include_keys,
            keyring_dir=keyring_dir,
            seal_config_path=seal_config_path,
        )
    )

    return out


# ---------------------------------------------------------------------------
# Per-component internals
# ---------------------------------------------------------------------------

def _absent(name: str) -> CollectedComponent:
    return CollectedComponent(
        members=[],
        coverage=CoverageEntry(component=name, status="absent", detail="store not present"),
    )


def _collect_merkle(
    home: Path, stage: Path, member_fn: Callable[..., BackupMember]
) -> CollectedComponent:
    """Snapshot the seal transparency log when it is local SQLite.

    A Postgres-backed Merkle log (Scale-S4) is server-side state: the local
    profile skips it honestly — and never records the DSN anywhere.
    """
    name = "seal-merkle"
    src = home / MERKLE_DB
    if not src.is_file():
        # Deliberately NEVER fall back to resolve_merkle_db_uri()'s
        # Path.home() default: *home* is the layout root being backed up, and
        # reaching outside it would slurp an unrelated deployment's log.
        # Only an EXPLICIT location is honored: the env override or the
        # home's own novaseal.yaml.
        uri = os.environ.get("NOVAFABRIC_SEAL_DB_PATH") or _merkle_uri_from_config(
            home / "novaseal.yaml"
        )
        if uri is None:
            return _absent(name)
        if uri.startswith(("postgresql://", "postgres://")):
            return CollectedComponent(
                members=[],
                coverage=CoverageEntry(
                    component=name,
                    status="skipped",
                    detail=(
                        "seal transparency log is Postgres-backed — covered by "
                        "the server-side backup (pg profile), not the local set"
                    ),
                ),
            )
        src = Path(uri).expanduser()
        if not src.is_file():
            return _absent(name)
    dst = stage / MERKLE_DB
    sqlite_snapshot(src, dst)
    return CollectedComponent(
        members=[member_fn(dst, MERKLE_DB, kind="state_db", role=name)],
        coverage=CoverageEntry(component=name, status="included"),
    )


def _collect_ratchet(
    home: Path, stage: Path, member_fn: Callable[..., BackupMember]
) -> CollectedComponent:
    name = "ratchet"
    src_dir = home / Path(*RATCHET_REL.parts)
    if not src_dir.is_dir():
        return _absent(name)
    members: list[BackupMember] = []
    for f in sorted(p for p in src_dir.rglob("*") if p.is_file()):
        arc = RATCHET_REL / f.relative_to(src_dir).as_posix()
        dst = stage / arc
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dst)
        # Per-node state files hold the current chain key (secret); the
        # epoch-registry holds public keys only.
        sensitive = f.name != "epoch-registry.jsonl"
        members.append(
            member_fn(dst, str(arc), kind="state_db", role=name, sensitive=sensitive)
        )
    if not members:
        return _absent(name)
    return CollectedComponent(
        members=members,
        coverage=CoverageEntry(component=name, status="included"),
    )


def _collect_dashboard(
    home: Path, stage: Path, member_fn: Callable[..., BackupMember]
) -> CollectedComponent:
    name = "dashboard"
    src = home / DASHBOARD_DUCKDB
    if not src.is_file():
        return _absent(name)
    dst = stage / DASHBOARD_DUCKDB
    skip_reason = duckdb_snapshot(src, dst)
    if skip_reason is not None:
        return CollectedComponent(
            members=[],
            coverage=CoverageEntry(component=name, status="skipped", detail=skip_reason),
        )
    return CollectedComponent(
        members=[member_fn(dst, DASHBOARD_DUCKDB, kind="state_db", role=name)],
        coverage=CoverageEntry(component=name, status="included"),
    )


def _collect_keys(
    home: Path,
    stage: Path,
    member_fn: Callable[..., BackupMember],
    *,
    include_keys: bool,
    keyring_dir: Optional[Path],
    seal_config_path: Optional[Path],
) -> CollectedComponent:
    name = "keys"
    if not include_keys:
        return CollectedComponent(
            members=[],
            coverage=CoverageEntry(
                component=name,
                status="excluded",
                detail=(
                    "key material excluded by default (ADR-0181 D3 / ADR-0216 D4); "
                    "pass --include-keys for a full-DR set"
                ),
            ),
        )

    members: list[BackupMember] = []

    resolved_keyring = resolve_origin_root("keyring", home, keyring_dir=keyring_dir)
    if resolved_keyring.is_dir():
        for f in sorted(resolved_keyring.glob("*.pem")):
            arc = f"{EXTERNAL_PREFIX}/keyring/{f.name}"
            dst = stage / arc
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst)
            members.append(
                member_fn(
                    dst, arc, kind="key_material", role=name,
                    origin="keyring", sensitive=True,
                )
            )

    config_src = seal_config_path if seal_config_path is not None else home / "novaseal.yaml"
    if config_src.is_file():
        for src_file in _seal_config_files(config_src):
            arc = f"{EXTERNAL_PREFIX}/seal-config/{src_file.name}"
            dst = stage / arc
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_file, dst)
            members.append(
                member_fn(
                    dst, arc, kind="key_material", role=name,
                    origin="seal-config", sensitive=True,
                )
            )

    if not members:
        return CollectedComponent(
            members=[],
            coverage=CoverageEntry(
                component=name,
                status="absent",
                detail="--include-keys given but no keyring or novaseal.yaml found",
            ),
        )
    return CollectedComponent(
        members=members,
        coverage=CoverageEntry(
            component=name,
            status="included",
            detail=f"{len(members)} key member(s) — set requires key-custody care",
        ),
    )


def _merkle_uri_from_config(config_path: Path) -> Optional[str]:
    """``merkle_db`` from the home's novaseal.yaml, or None."""
    if not config_path.is_file():
        return None
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text())
    except Exception:  # noqa: BLE001 — malformed config never breaks a backup
        return None
    if isinstance(raw, dict) and raw.get("merkle_db"):
        return str(raw["merkle_db"])
    return None


def _seal_config_files(config_path: Path) -> list[Path]:
    """novaseal.yaml plus the key/cert files it points at (when local)."""
    files = [config_path]
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text())
    except Exception:  # noqa: BLE001 — a malformed yaml still gets backed up itself
        return files
    if isinstance(raw, dict):
        for field in ("key_path", "cert_path"):
            value = raw.get(field)
            if value:
                candidate = Path(str(value)).expanduser()
                if candidate.is_file():
                    files.append(candidate)
    return files
