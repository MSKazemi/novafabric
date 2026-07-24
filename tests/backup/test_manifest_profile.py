"""ADR-0216 D6: the manifest-only object-store profile, end to end in memory.

Real chains are seeded through ManifestChainWriter/CheckpointCompactor into an
InMemoryWormAdapter, so hash linkage, checkpoints, and the rebuild path are
all genuine — no network, no bucket.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from novafabric.backup.create import BackupCreateError, create_backup
from novafabric.backup.models import PROFILE_MANIFEST_ONLY
from novafabric.backup.restore import restore_backup
from novafabric.backup.restore_manifest import BucketUnreachableError
from novafabric.backup.verify import verify_backup
from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.checkpoint import CheckpointCompactor
from novafabric.object_capsule_store.manifest_chain import (
    ManifestChainWriter,
    _version_key,
)

FAKE_SHA = "a" * 64


@pytest.fixture()
def _no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    from novafabric.registry.store import get_connection, init_schema

    home = tmp_path / "manifest-home"
    home.mkdir()
    conn = get_connection(home / "registry.db")
    init_schema(conn)
    conn.close()
    return home


@pytest.fixture()
def seeded_adapter() -> InMemoryWormAdapter:
    """2 tenants x 2 runs of real hash-linked chains, one with a checkpoint."""
    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)
    for tenant in ("tenant-a", "tenant-b"):
        for run in ("run-1", "run-2"):
            for i in range(4):
                payload = f"{tenant}/{run}/{i}".encode()
                sha = compute_sha256(payload)  # canonical CAS hash (FR-17)
                adapter.put_object(
                    f"capsules/{tenant}/{sha[:4]}/{sha}/data.zst",
                    payload,
                    sha256_hex=sha,
                    retention_days=1,
                )
                writer.append(
                    tenant,
                    run,
                    capsule_uri=f"capsules/{tenant}/{sha[:4]}/{sha}/data.zst",
                    capsule_sha256=sha,
                )
    CheckpointCompactor(adapter, checkpoint_every=2).force_checkpoint(
        "tenant-a", "run-1"
    )
    return adapter


@pytest.mark.usefixtures("_no_signing")
def test_manifest_profile_round_trip(
    home: Path, seeded_adapter: InMemoryWormAdapter, tmp_path: Path
) -> None:
    result = create_backup(
        tmp_path / "m.tar.gz", home=home, profile="manifest", adapter=seeded_adapter
    )
    manifest = result.manifest
    assert manifest.profile == PROFILE_MANIFEST_ONLY
    assert manifest.object_store_manifest == "object_store_manifest.json"
    assert manifest.object_store_backend == "local"
    assert manifest.object_store_fingerprint

    # No capsule blobs travel; the listing + registry + coverage rows do.
    paths = {m.path for m in manifest.members}
    assert "object_store_manifest.json" in paths
    assert not any(p.startswith("capsules/") for p in paths)
    status = {c.component: c.status for c in manifest.coverage}
    assert status["capsules"] == "skipped"
    assert status["object-store"] == "included"

    # Offline verify needs zero changes.
    assert verify_backup(result.archive_path).ok is True

    # Restore against the SAME adapter: chains verify, metadata rebuilds.
    home_b = tmp_path / "home-b"
    out = restore_backup(
        result.archive_path,
        home=home_b,
        adapter=seeded_adapter,
        audit_log_path=tmp_path / "audit-b" / "audit.jsonl",
        sample=3,
    )
    assert out.ok is True, [s for s in out.steps if not s.ok]
    by_name = {s.name: s for s in out.steps}
    assert "4 chain(s) verified" in by_name["verify-chains"].detail
    assert by_name["rebuild-metadata"].ok
    assert by_name["verify-capsule-sample"].ok

    rebuilt = home_b / "nova-metadata-rebuild.db"
    conn = sqlite3.connect(rebuilt)
    count = conn.execute("SELECT count(*) FROM capsules").fetchone()[0]
    conn.close()
    assert count == 16  # 2 tenants x 2 runs x 4 commits, all readable


@pytest.mark.usefixtures("_no_signing")
def test_listing_contains_no_secret_material(
    home: Path, seeded_adapter: InMemoryWormAdapter, tmp_path: Path, monkeypatch
) -> None:
    secret_bits = ("sk-ant-", "AKIA", "password", "postgres://", "postgresql://")
    result = create_backup(
        tmp_path / "m.tar.gz", home=home, profile="manifest", adapter=seeded_adapter
    )
    import tarfile

    with tarfile.open(result.archive_path, "r:gz") as tar:
        fh = tar.extractfile("object_store_manifest.json")
        assert fh is not None
        listing_text = fh.read().decode()
    for token in secret_bits:
        assert token not in listing_text


@pytest.mark.usefixtures("_no_signing")
def test_advanced_live_chain_still_passes(
    home: Path, seeded_adapter: InMemoryWormAdapter, tmp_path: Path
) -> None:
    result = create_backup(
        tmp_path / "m.tar.gz", home=home, profile="manifest", adapter=seeded_adapter
    )
    # The live chain advances after the backup — normal for append-only.
    writer = ManifestChainWriter(seeded_adapter)
    sha = compute_sha256(b"later")
    writer.append("tenant-a", "run-1", capsule_uri=f"capsules/tenant-a/{sha[:4]}/{sha}/data.zst", capsule_sha256=sha)

    out = restore_backup(
        result.archive_path,
        home=tmp_path / "home-b",
        adapter=seeded_adapter,
        audit_log_path=tmp_path / "audit-b" / "audit.jsonl",
    )
    assert out.ok is True
    step = next(s for s in out.steps if s.name == "verify-chains")
    assert "advanced past the backup" in step.detail


@pytest.mark.usefixtures("_no_signing")
def test_tampered_live_chain_fails_restore(
    home: Path, seeded_adapter: InMemoryWormAdapter, tmp_path: Path
) -> None:
    result = create_backup(
        tmp_path / "m.tar.gz", home=home, profile="manifest", adapter=seeded_adapter
    )
    # Rewrite a mid-chain commit in place (chain logs are NOT WORM-locked —
    # exactly the attack the signed head-pinning defends against).
    key = _version_key("tenant-b", "run-2", 2)
    commit = json.loads(seeded_adapter.get_object(key))
    commit["capsule_sha256"] = "b" * 64
    seeded_adapter._store[key] = json.dumps(commit).encode()  # type: ignore[attr-defined]

    out = restore_backup(
        result.archive_path,
        home=tmp_path / "home-b",
        adapter=seeded_adapter,
        audit_log_path=tmp_path / "audit-b" / "audit.jsonl",
    )
    assert out.ok is False
    step = next(s for s in out.steps if s.name == "verify-chains")
    assert step.ok is False and "tenant-b/run-2" in step.detail


@pytest.mark.usefixtures("_no_signing")
def test_unreachable_bucket_is_a_distinct_error(
    home: Path, seeded_adapter: InMemoryWormAdapter, tmp_path: Path
) -> None:
    result = create_backup(
        tmp_path / "m.tar.gz", home=home, profile="manifest", adapter=seeded_adapter
    )
    empty = InMemoryWormAdapter()  # a fresh adapter = the wrong/empty bucket
    with pytest.raises(BucketUnreachableError, match="not the bucket"):
        restore_backup(
            result.archive_path,
            home=tmp_path / "home-b",
            adapter=empty,
            audit_log_path=tmp_path / "audit-b" / "audit.jsonl",
        )


@pytest.mark.usefixtures("_no_signing")
def test_wal_guard_refuses_pending_uploads(
    home: Path, seeded_adapter: InMemoryWormAdapter, tmp_path: Path
) -> None:
    from novafabric.object_capsule_store.local_wal import LocalWal

    wal_db = tmp_path / "wal.db"
    wal = LocalWal(wal_db)
    wal.enqueue(
        capsule_uri=f"capsules/tenant-a/{FAKE_SHA[:4]}/{FAKE_SHA}/data.zst",
        capsule_sha256=FAKE_SHA,
        tenant="tenant-a",
        run_id="run-9",
    )
    with pytest.raises(BackupCreateError, match="[Dd]rain the WAL"):
        create_backup(
            tmp_path / "m.tar.gz",
            home=home,
            profile="manifest",
            adapter=seeded_adapter,
            wal_db_path=wal_db,
        )
    # Override records the gap as evidence instead.
    result = create_backup(
        tmp_path / "m2.tar.gz",
        home=home,
        profile="manifest",
        adapter=seeded_adapter,
        wal_db_path=wal_db,
        allow_pending_wal=True,
    )
    row = next(c for c in result.manifest.coverage if c.component == "object-store")
    assert "WAL upload(s) not yet chained" in (row.detail or "")


@pytest.mark.usefixtures("_no_signing")
def test_manifest_profile_requires_backend_or_adapter(
    home: Path, tmp_path: Path
) -> None:
    with pytest.raises(BackupCreateError, match="--backend"):
        create_backup(tmp_path / "m.tar.gz", home=home, profile="manifest")
