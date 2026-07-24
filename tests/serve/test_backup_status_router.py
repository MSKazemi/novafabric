"""Tests for the backup-set status read surface (ADR-0201 P7).

``GET /api/infra/backups`` lists NOVA_BACKUP_DIR archives via the read-only
``list_backup_sets`` inventory. Also unit-tests the inventory helper directly:
a manifest-claimed summary, honest reporting of a corrupt archive, newest-first
ordering, the limit/truncated bound, and the missing-directory / unconfigured
degradation. See src/novafabric/serve/routers/backup_status.py and
src/novafabric/backup/inventory.py.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.backup.create import MANIFEST_NAME  # noqa: E402
from novafabric.backup.inventory import list_backup_sets  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"


def _manifest_bytes(set_id: str, created_at: str) -> bytes:
    return json.dumps(
        {
            "set_id": set_id,
            "created_at": created_at,
            "nova_version": "0.63.0",
            "members": [
                {"path": "registry.db", "sha256": "a" * 64, "size_bytes": 2048, "kind": "registry"},
                {"path": "capsules/x.tar", "sha256": "b" * 64, "size_bytes": 1024, "kind": "blob"},
            ],
        }
    ).encode("utf-8")


def _write_archive(directory: Path, set_id: str, created_at: str) -> Path:
    archive = directory / f"nova-backup-{set_id}.tar.gz"
    raw = _manifest_bytes(set_id, created_at)
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
    return archive


class TestInventory:
    def test_summarizes_manifest(self, tmp_path: Path) -> None:
        _write_archive(tmp_path, "SET1", "2026-07-24T10:00:00+00:00")
        sets, truncated = list_backup_sets(tmp_path)
        assert truncated is False
        assert len(sets) == 1
        s = sets[0]
        assert s.ok is True
        assert s.set_id == "SET1"
        assert s.member_count == 2
        assert s.member_bytes == 3072
        assert s.signing_status == "unsigned"
        assert s.archive_bytes and s.archive_bytes > 0

    def test_newest_first(self, tmp_path: Path) -> None:
        _write_archive(tmp_path, "OLD", "2026-07-20T10:00:00+00:00")
        _write_archive(tmp_path, "NEW", "2026-07-24T10:00:00+00:00")
        sets, _ = list_backup_sets(tmp_path)
        assert [s.set_id for s in sets] == ["NEW", "OLD"]

    def test_corrupt_archive_reported_not_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "nova-backup-BROKEN.tar.gz").write_bytes(b"not a tar")
        sets, _ = list_backup_sets(tmp_path)
        assert len(sets) == 1
        assert sets[0].ok is False
        assert sets[0].error

    def test_limit_truncates(self, tmp_path: Path) -> None:
        for i in range(5):
            _write_archive(tmp_path, f"S{i}", f"2026-07-2{i}T10:00:00+00:00")
        sets, truncated = list_backup_sets(tmp_path, limit=3)
        assert len(sets) == 3
        assert truncated is True

    def test_missing_directory_is_empty(self, tmp_path: Path) -> None:
        sets, truncated = list_backup_sets(tmp_path / "nope")
        assert sets == []
        assert truncated is False


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    base = tmp_path / "runs"
    base.mkdir()
    app = create_app(
        token=VALID_TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


class TestRouter:
    def test_requires_token(self, client: TestClient) -> None:
        assert client.get("/api/infra/backups", headers=HEADERS).status_code == 401

    def test_unconfigured_degrades(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_BACKUP_DIR", raising=False)
        res = client.get(f"/api/infra/backups?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        assert res.json()["detected"] is False

    def test_lists_configured_backups(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bdir = tmp_path / "backups"
        bdir.mkdir()
        _write_archive(bdir, "SETA", "2026-07-24T10:00:00+00:00")
        monkeypatch.setenv("NOVA_BACKUP_DIR", str(bdir))
        res = client.get(f"/api/infra/backups?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        assert body["detected"] is True
        assert body["count"] == 1
        assert body["backups"][0]["set_id"] == "SETA"

    def test_nonexistent_dir_degrades(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_BACKUP_DIR", str(tmp_path / "nope"))
        res = client.get(f"/api/infra/backups?{TOKEN_Q}", headers=HEADERS)
        assert res.status_code == 200
        assert res.json()["detected"] is False
