"""``nova import``: a manifest ``capsule_id`` can only name a directory inside the store.

Review storage-backup-01 (reproduced): the manifest's ``capsule_id`` was joined
onto the store path unchecked, so a member with ``capsule_id`` set to an
absolute path or ``../..`` was unpacked *outside* the store, verified, and
reported ``imported: 1`` with exit 0. Two routes reach it: ``--allow-unsigned``
(the courier writes the manifest) and a signed export of a crafted capsule
whose ``capsule.yaml`` carries the path as its ``run_id``.

Every hostile id must refuse the whole import as INVALID (exit 3) before any
byte is written, with the store and everything around it unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from import_blob.helpers import (
    crafted_tar,
    export_capsules,
    make_capsule,
    manifest_path,
    read_manifest,
    write_signed_export,
)
from novafabric.evidence.signing import LocalSigner
from novafabric.export_blob.digest import compute_batch_digest
from novafabric.export_blob.models import ExportMember
from novafabric.import_blob.service import EXIT_INVALID, import_batch

_CAPSULE_TAR_ENTRIES = [
    ("capsule.yaml", b"schema_version: 0.1.0\nrun_id: planted\nstatus: success\n"),
    ("outputs/stdout.txt", b"planted"),
]


def _import(export_dir: Path, ctx: dict[str, Path], **kw: object):
    return import_batch(
        export_dir,
        capsule_root=ctx["store"],
        receipts_dir=ctx["receipts"],
        audit_log_path=ctx["audit"],
        db_path=ctx["registry"],
        **kw,
    )


@pytest.fixture()
def ctx(
    tmp_path: Path, store_root: Path, receipts_dir: Path, audit_path: Path, registry_db: Path
) -> dict[str, Path]:
    victim_dir = tmp_path / "victim_dir"
    victim_dir.mkdir()
    (victim_dir / "keep.txt").write_text("untouched")
    return {
        "root": tmp_path,
        "store": store_root,
        "receipts": receipts_dir,
        "audit": audit_path,
        "registry": registry_db,
        "victim_dir": victim_dir,
    }


def _tree(root: Path) -> set[str]:
    """Every path under *root*, minus the importer's own receipts/audit/registry and the export."""
    own = ("receipts", "audit.jsonl", "registry.db", "hostile")
    return {
        rel for rel in (p.relative_to(root).as_posix() for p in root.rglob("*"))
        if not rel.startswith(own)
    }


def _hostile_ids(ctx: dict[str, Path]) -> list[str]:
    return [
        str(ctx["victim_dir"] / "planted"),
        "../victim_dir/planted",
        "../../escaped",
        "sub/../../victim_dir/planted",
        "..\\victim_dir\\planted",
        "planted\x00",
        "..",
        ".",
    ]


def _assert_refused_and_nothing_written(outcome, ctx: dict[str, Path], before: set[str]) -> None:
    assert outcome.exit_code == EXIT_INVALID
    assert outcome.receipt.verification.status == "INVALID"
    assert any("capsule_id" in p for p in outcome.receipt.verification.problems), (
        outcome.receipt.verification.problems
    )
    assert outcome.receipt.counts.imported == 0
    assert list(ctx["store"].iterdir()) == [] or not (ctx["store"] / ".import-staging").exists()
    assert sorted(p.name for p in ctx["victim_dir"].iterdir()) == ["keep.txt"]
    assert _tree(ctx["root"]) == before


class TestSignedExportWithHostileCapsuleId:
    """A correctly signed export: the signature is valid, the id is still refused."""

    @pytest.mark.parametrize("index", range(8))
    def test_hostile_capsule_id_is_invalid_and_writes_nothing(
        self, index: int, ctx: dict[str, Path], signer: LocalSigner, public_pem: bytes
    ) -> None:
        capsule_id = _hostile_ids(ctx)[index]
        before = _tree(ctx["root"])
        export_dir = write_signed_export(
            ctx["root"] / "hostile", {capsule_id: crafted_tar(_CAPSULE_TAR_ENTRIES)}, signer
        )
        outcome = _import(export_dir, ctx, public_key_pem=public_pem)
        _assert_refused_and_nothing_written(outcome, ctx, before)

    def test_one_hostile_member_refuses_the_whole_batch(
        self, ctx: dict[str, Path], signer: LocalSigner, public_pem: bytes
    ) -> None:
        before = _tree(ctx["root"])
        tar = crafted_tar(_CAPSULE_TAR_ENTRIES)
        export_dir = write_signed_export(
            ctx["root"] / "hostile",
            {"01CLEAN0000000000000000001": tar + b"", "../victim_dir/planted": tar},
            signer,
        )
        outcome = _import(export_dir, ctx, public_key_pem=public_pem)
        _assert_refused_and_nothing_written(outcome, ctx, before)
        assert list(ctx["store"].iterdir()) == []

    def test_a_symlinked_store_entry_is_not_followed(
        self, ctx: dict[str, Path], signer: LocalSigner, public_pem: bytes
    ) -> None:
        """An existing store entry that is a link would be re-packed (read) through it."""
        (ctx["store"] / "linked").symlink_to(ctx["victim_dir"], target_is_directory=True)
        export_dir = write_signed_export(
            ctx["root"] / "hostile", {"linked": crafted_tar(_CAPSULE_TAR_ENTRIES)}, signer
        )
        outcome = _import(export_dir, ctx, public_key_pem=public_pem)
        assert outcome.exit_code == EXIT_INVALID
        assert any("symbolic link" in p for p in outcome.receipt.verification.problems)
        assert sorted(p.name for p in ctx["victim_dir"].iterdir()) == ["keep.txt"]

    def test_a_clean_capsule_id_still_imports(
        self, ctx: dict[str, Path], signer: LocalSigner, public_pem: bytes
    ) -> None:
        export_dir = write_signed_export(
            ctx["root"] / "hostile",
            {"01CLEAN0000000000000000001": crafted_tar(_CAPSULE_TAR_ENTRIES)},
            signer,
        )
        outcome = _import(export_dir, ctx, public_key_pem=public_pem)
        assert outcome.exit_code == 0, outcome.receipt.verification.problems
        assert outcome.receipt.counts.imported == 1
        assert (ctx["store"] / "01CLEAN0000000000000000001" / "capsule.yaml").is_file()


class TestUnsignedManifestWithHostileCapsuleId:
    """``--allow-unsigned``: the courier controls the manifest and recomputes the digest."""

    def test_absolute_and_dotdot_capsule_ids_are_invalid(
        self, ctx: dict[str, Path], source_root: Path, signer: LocalSigner
    ) -> None:
        export_dir = export_capsules(
            [make_capsule(source_root, "run-a", content="A")], ctx["root"] / "hostile", signer
        )
        for capsule_id in (str(ctx["victim_dir"] / "planted"), "../victim_dir/planted"):
            raw = read_manifest(export_dir)
            raw["members"][0]["capsule_id"] = capsule_id
            raw["batch_digest"] = compute_batch_digest(
                [ExportMember.model_validate(m) for m in raw["members"]]
            )
            manifest_path(export_dir).write_text(json.dumps(raw))
            before = _tree(ctx["root"])
            outcome = _import(export_dir, ctx, allow_unsigned=True)
            _assert_refused_and_nothing_written(outcome, ctx, before)
