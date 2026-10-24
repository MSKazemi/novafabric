"""Read-only collectors for the NF-277 retention-posture exporter (ADR-0159 D5).

Each function reads one shipped source and returns the typed facts
:func:`~.retention.build_retention_attestation` renders — nothing is inferred or defaulted into
existence:

* the registry's ``retention-policy.yaml`` via the ADR-0031
  :class:`~novafabric.storage._retention.RetentionPolicy` model, from every location
  ``nova retention`` reads (``$NOVAFABRIC_HOME/registries/<registry>/`` first, then the
  legacy ``./.novafabric/registries/<registry>/``);
* the legal-hold ledgers of the one hold store (:mod:`novafabric.retention.holds` —
  ``$NOVAFABRIC_HOME/registries/<registry>/holds.jsonl``, written by ``nova hold`` and
  ``POST /api/holds``, plus the pre-unification locations);
* the local ADR-0031 WORM adapter databases (``worm.db``, in the same locations) written by
  :class:`~novafabric.storage._local_worm.LocalWormAdapter`, queried through a ``mode=ro``
  connection (never the adapter, whose constructor runs DDL) and only when it already exists;
* operator-supplied cloud WORM receipts (the :class:`~novafabric.storage.worm.WormReceipt` an
  S3 / Azure / GCS adapter ``put`` returns — NovaFabric does not persist those itself);
* the Evidence Bundle ZIP: ``manifest.json`` timestamp fields and the ``manifest.dsse.tsr``
  RFC 3161 TimeStampResp written by ``nova export-evidence --timestamp`` (ADR-0030);
* the hash-chained audit log (:class:`~novafabric.audit.AuditLog`), opened only when it exists.

Unreadable or corrupt sealed evidence raises :class:`CorruptEvidenceError` (CLI exit 2); an
absent source is a gap the renderer reports as ``missing``, never an error.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .retention import AuditFacts, BundleFacts, HoldFacts, PolicyFacts, TimestampFacts, WormFacts

_MANIFEST = "manifest.json"
_TSR = "manifest.dsse.tsr"
#: Upper bounds on the bundle members read into memory (a manifest or TSR is KiB in practice).
_MANIFEST_MAX_BYTES = 16 * 1024 * 1024
_TSR_MAX_BYTES = 1024 * 1024


class CorruptEvidenceError(Exception):
    """A retention source exists but is unreadable or malformed (CLI exit 2)."""


def read_policy(registry_dir: Path) -> PolicyFacts | None:
    """Read ``retention-policy.yaml``; ``None`` when the registry has none.

    Raises:
        CorruptEvidenceError: the file exists but is not a valid ADR-0031 policy.
    """
    path = registry_dir / "retention-policy.yaml"
    if not path.exists():
        return None
    return _policy_facts(path)


def _policy_facts(path: Path) -> PolicyFacts:
    from novafabric.storage._retention import RetentionPolicy

    try:
        policy = RetentionPolicy.from_yaml(path)
    except (OSError, ValueError, ValidationError) as exc:
        raise CorruptEvidenceError(f"invalid retention policy {path}: {exc}") from exc
    except Exception as exc:  # yaml.YAMLError has no stable import-free base here
        raise CorruptEvidenceError(f"unreadable retention policy {path}: {exc}") from exc
    return PolicyFacts(
        ref=str(path),
        retention_days=policy.retention_days,
        deletion_mode=policy.deletion_mode,
        jurisdiction=policy.jurisdiction,
        legal_hold_ids=list(policy.legal_hold_ids),
    )


def read_registry_policy(
    registry: str, *, extra_roots: Iterable[Path] = ()
) -> PolicyFacts | None:
    """The retention policy of *registry*, read from every copy ``nova retention`` reads.

    Copies are found like holds (:func:`novafabric.retention.holds.registry_file_entries`):
    ``$NOVAFABRIC_HOME/registries/<registry>/`` first, then the legacy
    ``./.novafabric/registries/<registry>/`` (read, with a warning). The window and
    jurisdiction are the first copy's (the one ``nova retention`` takes bindings from);
    ``deletion_mode`` is ``prohibited`` when any copy says so, because deletion refuses
    then too; ``legal_hold_ids`` is the union; ``ref`` names every copy. ``None`` when
    the registry has no policy anywhere.

    Raises:
        CorruptEvidenceError: a copy is unreadable or invalid, a registry location
            cannot be examined, or *registry* is not a valid registry name.
    """
    from novafabric.retention.holds import (
        RETENTION_POLICY_FILENAME,
        HoldStoreError,
        canonical_registry_file,
        registry_file_entries,
        warn_legacy_registry_file,
    )

    try:
        paths = registry_file_entries(
            registry, RETENTION_POLICY_FILENAME, extra_roots=extra_roots
        )
        canonical = canonical_registry_file(registry, RETENTION_POLICY_FILENAME)
    except HoldStoreError as exc:
        raise CorruptEvidenceError(str(exc)) from exc
    copies: list[PolicyFacts] = []
    for path in paths:
        if path != canonical:
            warn_legacy_registry_file(path, canonical, used=True)
        copies.append(_policy_facts(path))
    if not copies:
        return None
    first = copies[0]
    modes = {c.deletion_mode for c in copies}
    return first.model_copy(
        update={
            "ref": "; ".join(c.ref for c in copies),
            "deletion_mode": "prohibited" if "prohibited" in modes else first.deletion_mode,
            "legal_hold_ids": list(
                dict.fromkeys(h for c in copies for h in c.legal_hold_ids)
            ),
        }
    )


def registry_worm_dbs(registry: str, *, extra_roots: Iterable[Path] = ()) -> list[Path]:
    """Every ``worm.db`` entry of *registry*, in the locations deletion reads them.

    Raises:
        CorruptEvidenceError: a registry location cannot be examined, or *registry*
            is not a valid registry name.
    """
    from novafabric.retention.holds import HoldStoreError
    from novafabric.retention.worm_state import WormStateUnavailableError, worm_db_paths

    try:
        return worm_db_paths(registry, extra_roots=extra_roots)
    except (HoldStoreError, WormStateUnavailableError) as exc:
        raise CorruptEvidenceError(str(exc)) from exc


def _hold_facts(ref: str, present: bool, records: Iterable[Any]) -> HoldFacts:
    active: list[str] = []
    released: list[str] = []
    for hold in records:
        if hold.corrupt:
            raise CorruptEvidenceError(f"invalid legal-hold ledger {hold.source}: {hold.reason}")
        (active if hold.active else released).append(hold.hold_id)
    return HoldFacts(
        ref=ref, present=present, active_hold_ids=active, released_hold_ids=released
    )


def read_holds(registry_dir: Path) -> HoldFacts:
    """Read one ``holds.jsonl`` legal-hold ledger (absent file = no hold ever recorded).

    Single-file reader kept for callers that name a registry directory; the CLI uses
    :func:`read_registry_holds`, which covers every hold location.

    Raises:
        CorruptEvidenceError: a line is not a JSON object carrying ``hold_id``.
    """
    from novafabric.retention.holds import HOLDS_FILENAME, read_ledger

    path = registry_dir / HOLDS_FILENAME
    if not path.exists():
        return HoldFacts(ref=str(path), present=False)
    return _hold_facts(str(path), True, read_ledger(path, registry_dir.name))


def read_registry_holds(
    registry: str, *, extra_roots: Iterable[Path] = ()
) -> HoldFacts:
    """Legal-hold state of *registry* from the one hold store (every hold location).

    ``ref`` names each ledger read (``; ``-separated), or the canonical ledger path when
    none exists (``present=False``).

    Raises:
        CorruptEvidenceError: a ledger line is unreadable, or *registry* is not a valid
            registry name.
    """
    from novafabric.retention.holds import (
        HOLDS_FILENAME,
        InvalidRegistryNameError,
        canonical_holds_root,
        read_holds,
    )

    try:
        records = read_holds(registry, extra_roots=extra_roots)
    except InvalidRegistryNameError as exc:
        raise CorruptEvidenceError(str(exc)) from exc
    sources = list(dict.fromkeys(str(h.source) for h in records))
    if not sources:
        return HoldFacts(
            ref=str(canonical_holds_root() / registry / HOLDS_FILENAME), present=False
        )
    return _hold_facts("; ".join(sources), True, records)


def read_local_worm(
    worm_db: Path, run_ids: Iterable[str], *, required: bool = False
) -> dict[str, WormFacts]:
    """Read local-adapter WORM locks for ``run_ids``; empty when ``worm_db`` does not exist.

    Same facts as ``LocalWormAdapter.list`` + ``verify_integrity`` (``sha256(data)`` against the
    stored digest), but over a ``mode=ro`` connection so the export never writes the store.
    *required* — the entry was found on disk (a discovered ``worm.db``), so one that is not a
    readable file (a dangling link, a directory) is corrupt evidence, not "no lock".
    """
    if not worm_db.exists():
        if required:
            raise CorruptEvidenceError(f"unreadable WORM database {worm_db}: not a file")
        return {}
    if required and not worm_db.is_file():
        raise CorruptEvidenceError(f"unreadable WORM database {worm_db}: not a file")
    facts: dict[str, WormFacts] = {}
    try:
        conn = sqlite3.connect(worm_db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for run_id in dict.fromkeys(run_ids):
                row = conn.execute(
                    "SELECT locked_until, data, sha256 FROM worm_capsules WHERE capsule_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    continue
                locked_until, data, stored_sha = row
                facts[run_id] = WormFacts(
                    ref=f"{worm_db}#{run_id}",
                    backend_type="local",
                    locked_until=datetime.fromisoformat(locked_until),
                    integrity_ok=hashlib.sha256(data).hexdigest() == stored_sha,
                )
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}  # an adapter database that never held a capsule
        raise CorruptEvidenceError(f"unreadable WORM database {worm_db}: {exc}") from exc
    except Exception as exc:  # sqlite3.DatabaseError / bad timestamps — corrupt evidence
        raise CorruptEvidenceError(f"unreadable WORM database {worm_db}: {exc}") from exc
    return facts


def read_worm_receipts(path: Path) -> dict[str, WormFacts]:
    """Read supplied cloud WORM receipts (a JSON object or list of ``WormReceipt`` objects).

    Raises:
        CorruptEvidenceError: the file is unreadable or a receipt is malformed.
    """
    from novafabric.storage.worm import WormReceipt

    try:
        doc: Any = json.loads(path.read_text(encoding="utf-8"))
        items = doc if isinstance(doc, list) else [doc]
        receipts = [WormReceipt.model_validate(item) for item in items]
    except (OSError, ValueError) as exc:  # ValidationError is a ValueError
        raise CorruptEvidenceError(f"invalid WORM receipts {path}: {exc}") from exc
    return {
        r.capsule_id: WormFacts(
            ref=f"{path}#{r.capsule_id}",
            backend_type=r.backend_type,
            locked_until=r.locked_until,
            confirmation=r.backend_confirmation_token,
        )
        for r in receipts
    }


def _read_member(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """Read ``name`` from ``zf``, refusing members larger than ``limit`` bytes.

    Checks the declared size first, then reads at most ``limit + 1`` bytes so a member whose
    header understates its size still cannot exhaust memory.

    Raises:
        KeyError: ``name`` is not in the archive.
        CorruptEvidenceError: the member exceeds ``limit``.
    """
    info = zf.getinfo(name)
    if info.file_size > limit:
        raise CorruptEvidenceError(f"{name} is {info.file_size} bytes, over the {limit}-byte limit")
    with zf.open(info) as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise CorruptEvidenceError(f"{name} exceeds the {limit}-byte limit")
    return data


def _tsr_facts(zf: zipfile.ZipFile, manifest: dict[str, Any]) -> TimestampFacts:
    from novafabric.trust._rfc3161 import TimestampError, _parse_pki_status

    base: dict[str, Any] = {
        "manifest_status": manifest.get("timestamp_status"),
        "failure_reason": manifest.get("timestamp_failure_reason"),
        "tsa_url": manifest.get("timestamp_tsa_url"),
        "manifest_tsr_sha256": manifest.get("manifest_dsse_tsr_sha256"),
    }
    if _TSR not in zf.namelist():
        return TimestampFacts(**base)
    tsr = _read_member(zf, _TSR, _TSR_MAX_BYTES)
    pki_status: int | None = None
    pki_error: str | None = None
    try:
        pki_status = _parse_pki_status(tsr)
    except TimestampError as exc:
        pki_error = str(exc)
    return TimestampFacts(
        **base,
        tsr_present=True,
        tsr_sha256="sha256:" + hashlib.sha256(tsr).hexdigest(),
        pki_status=pki_status,
        pki_error=pki_error,
    )


def read_bundle(path: Path) -> tuple[str | None, str, TimestampFacts]:
    """Read ``(bundle_id, run_id, timestamp facts)`` from an Evidence Bundle ZIP.

    Raises:
        CorruptEvidenceError: not a ZIP, no/invalid/oversized ``manifest.json``, an oversized
            ``manifest.dsse.tsr``, or no ``subject.run_id``.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(_read_member(zf, _MANIFEST, _MANIFEST_MAX_BYTES))
            if not isinstance(manifest, dict):
                raise ValueError("manifest.json is not an object")
            run_id = manifest.get("subject", {}).get("run_id")
            if not run_id:
                raise ValueError("manifest.json carries no subject.run_id")
            ts = _tsr_facts(zf, manifest)
    except (
        CorruptEvidenceError,
        OSError,
        KeyError,
        ValueError,
        AttributeError,
        zipfile.BadZipFile,
    ) as exc:
        raise CorruptEvidenceError(f"unreadable evidence bundle {path}: {exc}") from exc
    bundle_id = manifest.get("bundle_id")
    return (str(bundle_id) if bundle_id else None), str(run_id), ts


class AuditIndex:
    """The audit log read once: entries by ``resource_id`` plus the chain check result."""

    def __init__(self, log_path: Path) -> None:
        """Load and verify ``log_path`` (which must exist)."""
        from novafabric.audit import AuditLog

        log = AuditLog(log_path)
        self.ref = str(log_path)
        self.chain_errors = len(log.verify())
        self.parse_error: str | None = None
        self._by_resource: dict[str, list[str]] = {}
        try:
            for entry in log.query():
                self._by_resource.setdefault(entry.resource_id, []).append(entry.event_type.value)
        except ValueError as exc:  # a corrupt line: reported as partial, never hidden
            self.parse_error = str(exc)[:200]

    def facts_for(self, resource_ids: Iterable[str]) -> AuditFacts:
        """Audit facts for the entries whose ``resource_id`` is in ``resource_ids``."""
        events = [ev for rid in resource_ids for ev in self._by_resource.get(rid, [])]
        return AuditFacts(
            ref=self.ref,
            event_types=events,
            chain_errors=self.chain_errors,
            parse_error=self.parse_error,
        )


def collect_bundle_facts(
    bundles: Iterable[Path],
    *,
    worm_db: Path | None = None,
    worm_dbs: Iterable[Path] = (),
    worm_receipts: Path | None,
    audit_log: Path,
) -> list[BundleFacts]:
    """Read every bundle and join its WORM lock and audit trail by ``run_id``.

    *worm_db* is one operator-named database (absent = no lock); *worm_dbs* are discovered
    ``worm.db`` entries (:func:`registry_worm_dbs`), each of which must be readable. Across
    local databases the latest ``locked_until`` wins per run. A supplied cloud receipt wins over
    a local-adapter entry for the same run (the receipt is the stronger evidence); all are
    read-only.
    """
    read = [(p, *read_bundle(p)) for p in bundles]
    run_ids = [run_id for _, _, run_id, _ in read]
    worm: dict[str, WormFacts] = {}
    local = [(worm_db, False)] if worm_db is not None else []
    local += [(path, True) for path in dict.fromkeys(worm_dbs)]
    for path, required in local:
        for run_id, fact in read_local_worm(path, run_ids, required=required).items():
            current = worm.get(run_id)
            if current is None or fact.locked_until > current.locked_until:
                worm[run_id] = fact
    if worm_receipts is not None:
        worm.update(read_worm_receipts(worm_receipts))
    index = AuditIndex(audit_log) if audit_log.exists() else None
    facts: list[BundleFacts] = []
    for path, bundle_id, run_id, ts in read:
        keys = [run_id] + ([bundle_id] if bundle_id else [])
        facts.append(
            BundleFacts(
                bundle=path.name,
                bundle_id=bundle_id,
                run_id=run_id,
                timestamp=ts,
                worm=worm.get(run_id),
                audit=index.facts_for(keys) if index is not None else None,
                audit_log_ref=str(audit_log),
            )
        )
    return facts
