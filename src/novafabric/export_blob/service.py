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

"""Batch export + offline verification (ADR-0141 D2–D4).

``export_batch`` writes members content-addressed (idempotent → resumable) and
emits the signed manifest **last** as the atomic completion marker.
``verify_export_manifest`` is offline and total: DSSE signature, batch digest,
and every member's bytes at the destination.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import yaml
from cryptography.hazmat.primitives import serialization

from novafabric.capture._ulid import new_ulid
from novafabric.evidence.intoto import dsse_pae, dsse_sign_payload
from novafabric.evidence.signing import verify_with_pem
from novafabric.export_blob.destinations import (
    DestinationError,
    ExportDestination,
    LocalDirDestination,
    resolve_destination,
)
from novafabric.export_blob.digest import (
    canonical_signing_payload,
    compute_batch_digest,
    sort_members,
)
from novafabric.export_blob.models import (
    MANIFEST_FILENAME,
    MANIFEST_PAYLOAD_TYPE,
    SCHEMA_VERSION,
    DsseEnvelope,
    ExportManifest,
    ExportMember,
    Producer,
    WormIntent,
)
from novafabric.export_blob.packing import capsule_identifier, pack_capsule
from novafabric.object_capsule_store.cas import compute_sha256

log = logging.getLogger(__name__)


class ExportSelectionError(Exception):
    """The capsule selection cannot be resolved."""


class KeyringSigner:
    """Ed25519 signer over the existing NovaFabric keyring (``trust.keyring``).

    Satisfies the ``dsse_sign_payload`` signer protocol (``sign`` + ``keyid``),
    like :class:`novafabric.evidence.signing.LocalSigner` does for explicit
    ``--key`` PEM files. Reuses the keyring path — no new crypto surface.
    """

    def __init__(self, identity: str | None = None) -> None:
        from novafabric.trust.keyring import ensure_keypair

        key, fingerprint = ensure_keypair(identity)
        self._key = key
        self.keyid = f"keyring:ed25519:{fingerprint}"
        self.public_pem: bytes = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)


def _now_rfc3339() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _capsule_created_at(capsule_dir: Path) -> datetime | None:
    try:
        meta = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(meta, dict):
        return None
    raw = meta.get("created_at")
    return _parse_ts(str(raw)) if raw else None


@dataclass
class CapsuleSelection:
    """A frozen member set plus its provenance (ADR-0141 D1 / P4)."""

    capsule_dirs: list[Path]
    query: str | None
    query_resolved_at: str


def _run_ids_matching_query(query_text: str, root: Path) -> tuple[set[str], str]:
    """Run an ADR-0129 query as a **filter** and return the matching run ids.

    Deliberately uses the query's ``where`` clause rather than its grouping:
    ``run_id`` is not one of the DSL's allow-listed ``DIMENSIONS``, so
    "group by run_id" is not expressible — and adding it would both widen a
    documented public surface and invite exactly the high-cardinality grouping
    the DSL's cardinality cap exists to prevent. Filtering needs neither.

    Returns the run ids and the canonical query object as recorded provenance,
    so the manifest states what was asked, not merely what came back.
    """
    from novafabric.query.engine import QueryIndex  # noqa: PLC0415
    from novafabric.query.errors import QueryError  # noqa: PLC0415
    from novafabric.query.executor import resolve_time_window  # noqa: PLC0415
    from novafabric.query.indexer import scan_capsule_dir  # noqa: PLC0415
    from novafabric.query.parser import parse_where  # noqa: PLC0415

    # parse_where, not plan_from_query_object: a full plan requires a `select`
    # clause, and fabricating one here would put an aggregate the user never
    # asked for into the manifest's recorded provenance. Selection needs only
    # the predicates.
    try:
        predicates = parse_where(query_text)
    except QueryError as exc:
        raise ExportSelectionError(f"invalid --query: {exc}") from exc

    # Unbounded window: --since/--until are applied separately by the caller,
    # so the query must not silently impose a second, different one.
    since_epoch, until_epoch, _s, _u = resolve_time_window(
        None, None, datetime.now(timezone.utc)
    )
    index = QueryIndex.build(scan_capsule_dir(root), engine=None)
    try:
        rows = index.fetch_calls(predicates, since_epoch, until_epoch)
    except QueryError as exc:
        raise ExportSelectionError(f"--query failed: {exc}") from exc
    finally:
        index.close()

    run_ids = {str(row["run_id"]) for row in rows if row.get("run_id")}
    # Predicate.normalized() is the spec's own canonical rendering — use it
    # rather than hand-formatting, so the manifest and `nova query` agree on
    # how a predicate is written.
    recorded = json.dumps(
        {"where": [p.normalized() for p in predicates]}, sort_keys=True
    )
    return run_ids, recorded


def select_capsules(
    capsule_refs: list[str] | None = None,
    *,
    root: Path | None = None,
    since: str | None = None,
    until: str | None = None,
    query: str | None = None,
) -> CapsuleSelection:
    """Freeze the batch member set (a batch is a snapshot; spec §Export 1).

    Explicit *capsule_refs* (ids under *root* or paths) win and record
    ``query: null``. Otherwise *root* is scanned for capsule directories
    (containing ``capsule.yaml``), optionally filtered by ``created_at`` within
    ``[since, until]`` (inclusive) and/or by an ADR-0129 ``query`` filter.
    Selection is read-only.

    A ``query`` is applied **on top of** the time window, not instead of it:
    the two compose, and the recorded provenance names both.
    """
    resolved_root = root if root is not None else _default_capsule_root()
    resolved_at = _now_rfc3339()

    if capsule_refs:
        if since or until or query:
            raise ExportSelectionError(
                "--capsule is mutually exclusive with --since/--until/--query"
            )
        dirs: list[Path] = []
        for ref in capsule_refs:
            candidate = Path(ref)
            if not (candidate.is_dir() and (candidate / "capsule.yaml").is_file()):
                candidate = resolved_root / ref
            if not (candidate.is_dir() and (candidate / "capsule.yaml").is_file()):
                raise ExportSelectionError(
                    f"not a valid capsule (path or id under {resolved_root}): {ref}"
                )
            dirs.append(candidate)
        return CapsuleSelection(dirs, query=None, query_resolved_at=resolved_at)

    since_ts = _parse_ts(since) if since else None
    if since and since_ts is None:
        raise ExportSelectionError(f"invalid --since timestamp: {since!r}")
    until_ts = _parse_ts(until) if until else None
    if until and until_ts is None:
        raise ExportSelectionError(f"invalid --until timestamp: {until!r}")

    matching_run_ids: set[str] | None = None
    query_object: str | None = None
    if query:
        matching_run_ids, query_object = _run_ids_matching_query(query, resolved_root)

    dirs = []
    if resolved_root.is_dir():
        for child in sorted(resolved_root.iterdir()):
            if not (child.is_dir() and (child / "capsule.yaml").is_file()):
                continue
            if matching_run_ids is not None and child.name not in matching_run_ids:
                continue
            if since_ts or until_ts:
                created = _capsule_created_at(child)
                if created is None:
                    log.warning(
                        "capsule %s has no parseable created_at; "
                        "excluded from time-filtered export",
                        child.name,
                    )
                    continue
                if since_ts and created < since_ts:
                    continue
                if until_ts and created > until_ts:
                    continue
            dirs.append(child)

    parts = []
    if since:
        parts.append(f"created_at >= '{since}'")
    if until:
        parts.append(f"created_at <= '{until}'")
    if query_object is not None:
        # The canonical query object, not the raw string: provenance should
        # record what the DSL actually parsed, so a manifest reader is not
        # left re-deriving it from free text.
        parts.append(f"query={query_object}")
    provenance = " AND ".join(parts) if parts else "*"
    return CapsuleSelection(dirs, query=provenance, query_resolved_at=resolved_at)


def _default_capsule_root() -> Path:
    from novafabric._paths import default_capsule_dir

    return default_capsule_dir()


@dataclass
class ExportResult:
    """Outcome of one batch export."""

    manifest: ExportManifest
    written: int
    skipped: int


def _check_unsafe_skips(capsule_dir: Path) -> int:
    """Count ``unsafe_skips`` entries in the capsule's redaction proof (0 if absent)."""
    proof = capsule_dir / "redaction-proof.json"
    if not proof.is_file():
        return 0
    try:
        data = json.loads(proof.read_text())
    except (OSError, ValueError):
        return 0
    return len(data.get("unsafe_skips") or [])


class UnsafeSkipsExportError(RuntimeError):
    """A batch member's redaction proof records unsafe_skips (export refused)."""


def export_batch(
    selection: CapsuleSelection,
    dest: ExportDestination,
    signer: object,
    *,
    worm: WormIntent | None = None,
    allow_unsafe_skips: bool = False,
) -> ExportResult:
    """Write the frozen member set content-addressed, then the signed manifest last.

    Idempotent and resumable (spec §Export 2/4): blobs whose CAS address already
    exists at *dest* are skipped, and the manifest — the completion marker — is
    the final object written. Source capsules are never mutated.

    Mirrors the ``nova export-evidence`` redaction gate (THREAT_MODEL I-11): a
    member whose ``redaction-proof.json`` records ``unsafe_skips`` refuses the
    whole batch unless *allow_unsafe_skips* is set — exporting distributes the
    capsule bytes, so an incompletely-redacted capsule must not leave quietly.
    """
    members: list[ExportMember] = []
    payloads: dict[str, bytes] = {}
    for capsule_dir in selection.capsule_dirs:
        skips = _check_unsafe_skips(capsule_dir)
        if skips and not allow_unsafe_skips:
            raise UnsafeSkipsExportError(
                f"capsule {capsule_dir.name} has {skips} unsafe_skips entries in "
                "redaction-proof.json; pass --allow-unsafe-skips to export anyway"
            )
        data = pack_capsule(capsule_dir)
        content_hash = "sha256:" + compute_sha256(data)
        members.append(
            ExportMember(
                capsule_id=capsule_identifier(capsule_dir),
                content_hash=content_hash,
                size=len(data),
            )
        )
        payloads[content_hash] = data
    members = sort_members(members)

    written = skipped = 0
    for content_hash, data in sorted(payloads.items()):
        if dest.blob_exists(content_hash):
            skipped += 1
            continue
        dest.put_blob(content_hash, data, worm=worm is not None)
        written += 1

    batch_digest = compute_batch_digest(members)
    export_id = new_ulid()
    count = len(members)
    payload = canonical_signing_payload(
        schema_version=SCHEMA_VERSION,
        export_id=export_id,
        dest=dest.uri,
        members=members,
        count=count,
        batch_digest=batch_digest,
    )
    envelope = dsse_sign_payload(payload, MANIFEST_PAYLOAD_TYPE, signer)

    manifest = ExportManifest(
        export_id=export_id,
        created_at=_now_rfc3339(),
        dest=dest.uri,
        members=members,
        count=count,
        batch_digest=batch_digest,
        signature=DsseEnvelope.model_validate(envelope),
        query=selection.query,
        query_resolved_at=selection.query_resolved_at,
        worm=worm,
        producer=Producer(version=_pkg_version()),
    )
    manifest_bytes = (
        json.dumps(manifest.to_json_dict(), indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    # Written LAST — the manifest's presence means "this batch is complete".
    dest.put_manifest(MANIFEST_FILENAME, manifest_bytes, worm=worm is not None)
    return ExportResult(manifest=manifest, written=written, skipped=skipped)


def _pkg_version() -> str:
    try:
        return version("novafabric")
    except PackageNotFoundError:  # pragma: no cover — dev tree without metadata
        return "0.0.0"


class VerifyStatus(str, Enum):
    """Spec §Verify verdicts."""

    VALID = "VALID"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"


@dataclass
class VerifyReport:
    """Outcome of an offline manifest verification."""

    status: VerifyStatus
    problems: list[str] = field(default_factory=list)
    members_total: int = 0
    members_ok: int = 0


def verify_export_manifest(
    manifest_path: Path,
    public_key_pem: bytes,
    *,
    dest_override: str | None = None,
    s3_client: object | None = None,
) -> VerifyReport:
    """Offline, total verification of an export manifest (spec §Verify).

    ``INVALID`` — bad structure, DSSE signature, count, or batch digest.
    ``INCOMPLETE`` — a member missing at the destination, or size/hash mismatch.
    ``VALID`` — signature + digest + every member check pass.
    """
    try:
        raw = json.loads(manifest_path.read_text())
        manifest = ExportManifest.model_validate(raw)
    except (OSError, ValueError) as exc:
        return VerifyReport(VerifyStatus.INVALID, [f"manifest unreadable/invalid: {exc}"])

    problems: list[str] = []

    # 1. DSSE signature over the reconstructed canonical payload.
    payload = canonical_signing_payload(
        schema_version=manifest.schema_version,
        export_id=manifest.export_id,
        dest=manifest.dest,
        members=manifest.members,
        count=manifest.count,
        batch_digest=manifest.batch_digest,
    )
    envelope = manifest.signature
    if envelope.payload is not None and base64.b64decode(envelope.payload) != payload:
        problems.append("embedded DSSE payload does not match the manifest fields")
    if not problems:
        pae = dsse_pae(envelope.payloadType, payload)
        try:
            sig = base64.b64decode(envelope.signatures[0].sig)
            if not verify_with_pem(public_key_pem, pae, sig):
                problems.append("DSSE signature verification failed")
        except ValueError as exc:
            problems.append(f"signature check failed: {exc}")

    # 2. Internal consistency: count and batch digest.
    if manifest.count != len(manifest.members):
        problems.append(
            f"count={manifest.count} does not equal len(members)={len(manifest.members)}"
        )
    recomputed = compute_batch_digest(manifest.members)
    if recomputed != manifest.batch_digest:
        problems.append(
            f"batch_digest mismatch: stored {manifest.batch_digest}, "
            f"recomputed {recomputed}"
        )
    if problems:
        return VerifyReport(
            VerifyStatus.INVALID, problems, members_total=len(manifest.members)
        )

    # 3. Every member's bytes at the destination.
    try:
        dest = _resolve_verify_destination(
            manifest, manifest_path, dest_override, s3_client=s3_client
        )
    except DestinationError as exc:
        return VerifyReport(
            VerifyStatus.INCOMPLETE,
            [f"destination unavailable: {exc}"],
            members_total=len(manifest.members),
        )

    members_ok = 0
    for member in manifest.members:
        data = dest.get_blob(member.content_hash)
        if data is None:
            problems.append(f"member missing at dest: {member.capsule_id} ({member.content_hash})")
            continue
        actual_hash = "sha256:" + compute_sha256(data)
        if actual_hash != member.content_hash:
            problems.append(
                f"member content mismatch: {member.capsule_id} "
                f"expected {member.content_hash}, got {actual_hash}"
            )
            continue
        if len(data) != member.size:
            problems.append(
                f"member size mismatch: {member.capsule_id} "
                f"expected {member.size}, got {len(data)}"
            )
            continue
        members_ok += 1

    status = VerifyStatus.VALID if not problems else VerifyStatus.INCOMPLETE
    return VerifyReport(
        status, problems, members_total=len(manifest.members), members_ok=members_ok
    )


def _resolve_verify_destination(
    manifest: ExportManifest,
    manifest_path: Path,
    dest_override: str | None,
    *,
    s3_client: object | None,
) -> ExportDestination:
    """Destination to read member bytes from during verification.

    Priority: explicit override → the manifest's recorded ``dest`` → (for local
    paths that don't resolve, e.g. a manifest moved off-box) the directory the
    manifest file itself sits in, since the manifest is written at the
    destination root.
    """
    if dest_override:
        return resolve_destination(dest_override, s3_client=s3_client)
    if manifest.dest.startswith(("s3://", "azure://", "gcs://")):
        return resolve_destination(manifest.dest, s3_client=s3_client)
    recorded = Path(manifest.dest)
    if (recorded / "objects").is_dir():
        return LocalDirDestination(recorded, uri=manifest.dest)
    return LocalDirDestination(manifest_path.parent, uri=str(manifest_path.parent))
