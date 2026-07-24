"""Test helpers for the batch import suite (ADR-0207).

Fixtures are built by round-tripping the **real exporter** (``export_batch``)
over real capsule directories, so import tests exercise the actual interchange
layout, not a hand-rolled imitation. Tamper/hardening cases then modify that
honest output (or hand-sign a crafted layout with the same DSSE primitives the
exporter uses).
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import yaml

from novafabric.capture._ulid import new_ulid
from novafabric.evidence.intoto import dsse_sign_payload
from novafabric.export_blob.destinations import LocalDirDestination, blob_key
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
)
from novafabric.export_blob.service import CapsuleSelection, export_batch
from novafabric.object_capsule_store.cas import compute_sha256


def make_capsule(
    root: Path,
    run_id: str,
    *,
    created_at: str = "2026-07-01T00:00:00Z",
    content: str = "hello",
    status: str = "success",
    parent_run_id: str | None = None,
    with_lineage: bool = False,
) -> Path:
    """A minimal on-disk capsule directory (optionally with real lineage)."""
    capsule = root / run_id
    (capsule / "outputs").mkdir(parents=True)
    meta: dict[str, object] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": status,
        "created_at": created_at,
        "command": ["python", "-c", "pass"],
    }
    if parent_run_id is not None:
        meta["parent_run_id"] = parent_run_id
    (capsule / "capsule.yaml").write_text(yaml.dump(meta))
    (capsule / "outputs" / "stdout.txt").write_text(content)
    if with_lineage:
        (capsule / "assets.jsonl").write_text(
            json.dumps(
                {
                    "asset_ref": "model:foo@1.0.0+sha256:abc",
                    "registry": "local",
                    "asset_type": "model",
                    "name": "foo",
                    "version": "1.0.0",
                }
            )
            + "\n"
        )
        from novafabric.lineage._writer import LineageWriter

        writer = LineageWriter(capsule_dir=capsule, run_id=run_id)
        writer.write(writer.infer())
    return capsule


def export_capsules(capsule_dirs: list[Path], dest: Path, signer: object) -> Path:
    """Run the real exporter over *capsule_dirs*; return the export dir."""
    selection = CapsuleSelection(
        capsule_dirs=capsule_dirs,
        query=None,
        query_resolved_at="2026-07-24T00:00:00.000000Z",
    )
    export_batch(selection, LocalDirDestination(dest, uri=str(dest)), signer)
    return dest


def manifest_path(export_dir: Path) -> Path:
    return export_dir / MANIFEST_FILENAME


def blob_path(export_dir: Path, content_hash: str) -> Path:
    return export_dir / blob_key(content_hash)


def read_manifest(export_dir: Path) -> dict:
    return json.loads(manifest_path(export_dir).read_text())


def crafted_tar(entries: list[tarfile.TarInfo | tuple[str, bytes]]) -> bytes:
    """Tar bytes from raw entries — used to craft hostile member blobs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for entry in entries:
            if isinstance(entry, tuple):
                name, data = entry
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            else:
                tf.addfile(entry)
    return buf.getvalue()


def write_signed_export(
    dest: Path, blobs: dict[str, bytes], signer: object
) -> Path:
    """A correctly-signed export layout over arbitrary (possibly hostile) blobs.

    The manifest is honest about the blob bytes (hashes match), so verification
    passes and the *unpack* hardening is what is under test.
    """
    dest.mkdir(parents=True, exist_ok=True)
    members = sort_members(
        [
            ExportMember(
                capsule_id=capsule_id,
                content_hash="sha256:" + compute_sha256(data),
                size=len(data),
            )
            for capsule_id, data in blobs.items()
        ]
    )
    for capsule_id, data in blobs.items():
        path = dest / blob_key("sha256:" + compute_sha256(data))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    batch_digest = compute_batch_digest(members)
    export_id = new_ulid()
    payload = canonical_signing_payload(
        schema_version=SCHEMA_VERSION,
        export_id=export_id,
        dest=str(dest),
        members=members,
        count=len(members),
        batch_digest=batch_digest,
    )
    envelope = dsse_sign_payload(payload, MANIFEST_PAYLOAD_TYPE, signer)
    manifest = ExportManifest(
        export_id=export_id,
        created_at="2026-07-24T00:00:00.000000Z",
        dest=str(dest),
        members=members,
        count=len(members),
        batch_digest=batch_digest,
        signature=DsseEnvelope.model_validate(envelope),
    )
    manifest_file = dest / MANIFEST_FILENAME
    manifest_file.write_text(json.dumps(manifest.to_json_dict(), indent=2) + "\n")
    return dest
