"""Redaction gate on batch export (THREAT_MODEL I-11 fix).

A batch member whose ``redaction-proof.json`` records ``unsafe_skips`` refuses
the whole export unless ``allow_unsafe_skips=True`` — mirroring the
``nova export-evidence`` gate.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from export_blob.helpers import make_capsule
from novafabric.evidence.signing import LocalSigner
from novafabric.export_blob.destinations import LocalDirDestination
from novafabric.export_blob.service import (
    CapsuleSelection,
    UnsafeSkipsExportError,
    export_batch,
)


def _selection(dirs: list[Path]) -> CapsuleSelection:
    return CapsuleSelection(
        capsule_dirs=dirs,
        query=None,
        query_resolved_at="2026-07-16T00:00:00.000000Z",
    )


def _write_proof(capsule: Path, unsafe_skips: list[dict] | None) -> None:
    proof: dict = {"schema_version": "0.1.0", "findings": []}
    if unsafe_skips is not None:
        proof["unsafe_skips"] = unsafe_skips
    (capsule / "redaction-proof.json").write_text(json.dumps(proof))


def test_unsafe_skips_member_refuses_batch(tmp_path: Path, signer: LocalSigner) -> None:
    clean = make_capsule(tmp_path / "caps", "run-clean")
    dirty = make_capsule(tmp_path / "caps", "run-dirty")
    _write_proof(dirty, [{"path": "outputs/x", "rule": "r1"}])
    dest = LocalDirDestination(tmp_path / "dest", uri=str(tmp_path / "dest"))

    with pytest.raises(UnsafeSkipsExportError, match="run-dirty"):
        export_batch(_selection([clean, dirty]), dest, signer)
    # Refusal happens before any blob is written for the batch to stay atomic
    # w.r.t. the gate (the clean member is enumerated first but the gate runs
    # during member collection, prior to destination writes).
    assert not (tmp_path / "dest" / "export-manifest.json").exists()


def test_allow_unsafe_skips_waives_the_gate(tmp_path: Path, signer: LocalSigner) -> None:
    dirty = make_capsule(tmp_path / "caps", "run-dirty")
    _write_proof(dirty, [{"path": "outputs/x", "rule": "r1"}])
    dest = LocalDirDestination(tmp_path / "dest", uri=str(tmp_path / "dest"))

    result = export_batch(
        _selection([dirty]), dest, signer, allow_unsafe_skips=True
    )
    assert result.manifest.count == 1


def test_clean_or_absent_proof_exports_normally(
    tmp_path: Path, signer: LocalSigner
) -> None:
    no_proof = make_capsule(tmp_path / "caps", "run-none")
    empty_skips = make_capsule(tmp_path / "caps", "run-empty")
    _write_proof(empty_skips, [])
    dest = LocalDirDestination(tmp_path / "dest", uri=str(tmp_path / "dest"))

    result = export_batch(_selection([no_proof, empty_skips]), dest, signer)
    assert result.manifest.count == 2
