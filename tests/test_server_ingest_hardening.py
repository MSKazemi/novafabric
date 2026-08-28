"""ADR-0203 P1 — server ingest hardening (experimental).

Covers, against ``POST /v0/capsules``:

- the zip-slip path-traversal defect recorded in ADR-0203 Context §4
  (member ``a/../../evil`` written outside the capsule store) — written
  FIRST, against the pre-hardening code, to confirm the reading;
- the wedged-run_id defect (crash mid-extract leaves a partial capsule dir,
  every retry then 409s) — ADR-0203 D2 atomic temp-dir + rename;
- ``max_upload_bytes`` → 413 ``payload_too_large`` with the ADR-0017 envelope;
- zip-bomb guards (entry count / total uncompressed / compression ratio) →
  422 ``zip_guard_violation`` with ``details.reason``;
- ``0`` escape hatches disabling each guard;
- happy-path upload regression pins (201 shape, 409 duplicate,
  409 parent_not_found).

Fixture archives follow the normative names in
``design/spec/ingest-hardening-v0.md`` but are generated in-test (caps are
lowered via config so fixtures stay small).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import IngestConfig, ServerConfig  # noqa: E402

# --------------------------------------------------------------------------- #
# Archive builders (in-memory; spec fixture vocabulary)
# --------------------------------------------------------------------------- #


def _manifest(run_id: str, **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.6.12",
        "run_id": run_id,
        "created_at": "2026-04-15T10:00:00+00:00",
        "finished_at": "2026-04-15T10:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print('hi')"],
        "exit_code": 0,
        "status": "success",
    }
    doc.update(extra)
    return doc


def _zip_bytes(members: dict[str, bytes], *, stored: bool = False) -> bytes:
    """Build an in-memory ZIP with *members* in insertion order."""
    buf = io.BytesIO()
    compression = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _valid_capsule_zip(
    run_id: str,
    extra_members: dict[str, bytes] | None = None,
    *,
    stored: bool = False,
    manifest_extra: dict[str, Any] | None = None,
) -> bytes:
    members: dict[str, bytes] = {
        "capsule.yaml": yaml.safe_dump(_manifest(run_id, **(manifest_extra or {}))).encode()
    }
    members.update(extra_members or {})
    return _zip_bytes(members, stored=stored)


def _upload(client: TestClient, data: bytes, name: str = "capsule.zip") -> Any:
    return client.post(
        "/v0/capsules",
        files={"capsule": (name, io.BytesIO(data), "application/zip")},
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    cdir = tmp_path / "runs"
    cdir.mkdir()
    return cdir


@pytest.fixture
def make_client(tmp_path: Path, capsule_dir: Path) -> Any:
    """Factory: build a TestClient with an optional ``ingest`` config block."""

    def _make(ingest: dict[str, Any] | None = None) -> TestClient:
        from novafabric.server import deps

        raw: dict[str, Any] = {
            "db_path": str(tmp_path / "test.db"),
            "insecure_no_auth": True,
        }
        if ingest is not None:
            raw["ingest"] = ingest
        cfg = ServerConfig.model_validate(raw)
        app = create_app(cfg)
        app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
        return TestClient(app, raise_server_exceptions=False)

    return _make


@pytest.fixture
def client(make_client: Any) -> TestClient:
    return make_client()


# --------------------------------------------------------------------------- #
# Defect 1 — zip-slip path traversal (ADR-0203 Context §4; written first)
# --------------------------------------------------------------------------- #


class TestZipSlip:
    def test_zipslip_member_rejected_and_nothing_written_outside(
        self, client: TestClient, capsule_dir: Path, tmp_path: Path
    ) -> None:
        """A member named ``a/../../evil`` must be rejected (422
        ``zip_guard_violation`` / ``unsafe_member_name``) and must never be
        written outside the capsule store.

        Pre-hardening this resolves to ``capsule_dir/<run_id>/../../evil`` ==
        ``tmp_path/evil`` — outside the store — confirming the ADR reading.
        """
        data = _valid_capsule_zip("slip-run", {"a/../../evil": b"pwned"})
        resp = _upload(client, data, "zipslip.zip")

        escaped = tmp_path / "evil"
        assert not escaped.exists(), "zip-slip wrote outside the capsule store"
        assert resp.status_code == 422
        body = resp.json()["error"]
        assert body["code"] == "zip_guard_violation"
        assert body["details"]["reason"] == "unsafe_member_name"
        # Nothing (not even a partial dir) may exist for the run.
        assert not (capsule_dir / "slip-run").exists()

    def test_dotdot_only_member_rejected(
        self, client: TestClient, capsule_dir: Path, tmp_path: Path
    ) -> None:
        data = _valid_capsule_zip("slip-run2", {"nested/..": b"x", "nested/../oops": b"x"})
        resp = _upload(client, data)
        assert resp.status_code == 422
        assert resp.json()["error"]["details"]["reason"] == "unsafe_member_name"
        assert not (capsule_dir / "slip-run2").exists()
        assert not (tmp_path / "oops").exists()


# --------------------------------------------------------------------------- #
# Defect 2 — crash mid-extract wedges the run_id (ADR-0203 D2)
# --------------------------------------------------------------------------- #


class TestAtomicIngest:
    def test_crash_mid_extract_does_not_wedge_run_id(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        """A genuine mid-extract crash (file/dir collision) must not leave a
        partial ``capsule_dir/<run_id>`` behind; retrying with a clean archive
        must succeed instead of hitting the duplicate 409.
        """
        # sub/x is written as a file, then sub/x/y needs sub/x to be a
        # directory -> FileExistsError mid-extract, after capsule.yaml landed.
        bad = _valid_capsule_zip("wedge-run", {"sub/x": b"file", "sub/x/y": b"boom"})
        resp1 = _upload(client, bad)
        assert resp1.status_code >= 500  # the crash itself

        # No partial capsule directory may remain.
        assert not (capsule_dir / "wedge-run").exists()

        # Retry with a clean archive: must be 201, not the 409 wedge.
        good = _valid_capsule_zip("wedge-run", {"trace.jsonl": b""})
        resp2 = _upload(client, good)
        assert resp2.status_code == 201, resp2.text
        assert (capsule_dir / "wedge-run" / "capsule.yaml").exists()

    def test_no_spool_or_tempdir_leftovers(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        """Spool files and temp extract dirs are removed on success AND failure."""
        _upload(client, _valid_capsule_zip("clean-run", {"trace.jsonl": b"{}"}))
        _upload(client, b"not a zip")  # 400 path
        _upload(client, _valid_capsule_zip("slip", {"a/../../e": b"x"}))  # 422 path
        spool_dir = capsule_dir / ".ingest-tmp"
        leftovers = list(spool_dir.iterdir()) if spool_dir.exists() else []
        assert leftovers == []

    def test_traversal_run_id_rejected(
        self, client: TestClient, capsule_dir: Path, tmp_path: Path
    ) -> None:
        """A hostile run_id is a path component of the store — reject it."""
        data = _valid_capsule_zip("../evil-run")
        resp = _upload(client, data)
        assert resp.status_code == 400
        assert not (tmp_path / "evil-run").exists()


# --------------------------------------------------------------------------- #
# D1 — max_upload_bytes → 413 payload_too_large
# --------------------------------------------------------------------------- #


class TestUploadSizeCap:
    def test_oversized_upload_413_with_envelope(
        self, make_client: Any, capsule_dir: Path
    ) -> None:
        client = make_client({"max_upload_bytes": 1000})
        data = _valid_capsule_zip("big-run", {"pad.bin": b"\xff" * 5000}, stored=True)
        resp = _upload(client, data, "oversized.zip")
        assert resp.status_code == 413
        body = resp.json()["error"]
        assert body["code"] == "payload_too_large"
        assert body["details"]["limit_bytes"] == 1000
        assert body["details"]["received_bytes"] > 1000
        assert not (capsule_dir / "big-run").exists()

    def test_exactly_at_limit_passes(self) -> None:
        """The counted-stream path admits exactly max_upload_bytes."""
        import asyncio

        from starlette.datastructures import UploadFile as StarletteUploadFile

        from novafabric.server.ingest import spool_upload

        data = b"x" * 4096
        limits = IngestConfig(max_upload_bytes=4096)

        async def run() -> tuple[Path, int]:
            upload = StarletteUploadFile(file=io.BytesIO(data), filename="c.zip")
            return await spool_upload(upload, self._spool_dir, limits)

        spool_path, received = asyncio.run(run())
        try:
            assert received == 4096
            assert spool_path.read_bytes() == data
        finally:
            spool_path.unlink()

    @pytest.fixture(autouse=True)
    def _spool(self, tmp_path: Path) -> None:
        self._spool_dir = tmp_path / "spool"

    def test_stream_path_catches_lying_content_length(self) -> None:
        """Even without a (truthful) Content-Length, the counted stream 413s
        and removes the partial spool file."""
        import asyncio

        from starlette.datastructures import UploadFile as StarletteUploadFile

        from novafabric.server.errors import PayloadTooLargeError
        from novafabric.server.ingest import spool_upload

        limits = IngestConfig(max_upload_bytes=100)

        async def run() -> None:
            upload = StarletteUploadFile(file=io.BytesIO(b"y" * 250), filename="c.zip")
            await spool_upload(upload, self._spool_dir, limits)

        with pytest.raises(PayloadTooLargeError) as exc_info:
            asyncio.run(run())
        assert exc_info.value.details == {"limit_bytes": 100, "received_bytes": 250}
        assert list(self._spool_dir.iterdir()) == []

    def test_spool_reads_are_chunk_bounded(self) -> None:
        """Structural memory bound: the body is read in ≤ spool_chunk_bytes
        chunks, never in one gulp."""
        import asyncio

        from novafabric.server.ingest import spool_upload

        requested: list[int] = []
        payload = io.BytesIO(b"z" * (200 * 1024))

        class RecordingUpload:
            async def read(self, size: int = -1) -> bytes:
                requested.append(size)
                return payload.read(size)

        limits = IngestConfig(spool_chunk_bytes=65_536)
        spool_path, received = asyncio.run(
            spool_upload(RecordingUpload(), self._spool_dir, limits)  # type: ignore[arg-type]
        )
        spool_path.unlink()
        assert received == 200 * 1024
        assert max(requested) <= 65_536
        assert len(requested) >= 4  # genuinely chunked

    def test_cap_zero_disables(self, make_client: Any, capsule_dir: Path) -> None:
        client = make_client({"max_upload_bytes": 0})
        data = _valid_capsule_zip("uncapped-run", {"pad.bin": b"\xfe" * 10_000})
        resp = _upload(client, data)
        assert resp.status_code == 201
        assert (capsule_dir / "uncapped-run" / "pad.bin").exists()

    def test_content_length_fast_path_rejects_without_spooling(
        self, make_client: Any, capsule_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the declared Content-Length already exceeds the cap, our spool
        path must never engage."""
        from novafabric.server import ingest as ingest_mod

        async def _fail(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("spool_upload must not be called on the fast path")

        monkeypatch.setattr(ingest_mod, "spool_upload", _fail)
        client = make_client({"max_upload_bytes": 500})
        resp = _upload(client, _valid_capsule_zip("fp-run", {"p.bin": b"\xfd" * 2000}))
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "payload_too_large"


# --------------------------------------------------------------------------- #
# D3 — zip-bomb guards → 422 zip_guard_violation
# --------------------------------------------------------------------------- #


def _assert_guard(resp: Any, reason: str) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()["error"]
    assert body["code"] == "zip_guard_violation"
    assert body["details"]["reason"] == reason


class TestZipBombGuards:
    def test_high_ratio_bomb_rejected(
        self, make_client: Any, capsule_dir: Path
    ) -> None:
        client = make_client({"zip_max_ratio": 5})
        # 3 MiB of zeros deflates ~1000:1 and is past the 1 MiB ratio floor.
        data = _valid_capsule_zip("bomb-run", {"big.bin": b"\x00" * (3 * 1024 * 1024)})
        resp = _upload(client, data, "bomb-ratio.zip")
        _assert_guard(resp, "compression_ratio")
        assert resp.json()["error"]["details"]["member"] == "big.bin"
        assert not (capsule_dir / "bomb-run").exists()

    def test_ratio_floor_spares_tiny_compressible_members(
        self, make_client: Any, capsule_dir: Path
    ) -> None:
        client = make_client({"zip_max_ratio": 5})
        # 100 KiB of zeros also deflates far past 5:1 — but under the 1 MiB
        # floor it must NOT trip the guard (normative in the spec).
        data = _valid_capsule_zip("floor-run", {"small.bin": b"\x00" * (100 * 1024)})
        resp = _upload(client, data)
        assert resp.status_code == 201, resp.text

    def test_many_entries_bomb_rejected(
        self, make_client: Any, capsule_dir: Path
    ) -> None:
        client = make_client({"zip_max_entries": 5})
        members = {f"f{i}.txt": b"x" for i in range(6)}
        resp = _upload(client, _valid_capsule_zip("entries-run", members), "bomb-entries.zip")
        _assert_guard(resp, "entry_count")
        assert not (capsule_dir / "entries-run").exists()

    def test_total_uncompressed_bomb_rejected(
        self, make_client: Any, capsule_dir: Path
    ) -> None:
        import os as _os

        client = make_client({"zip_max_uncompressed_bytes": 1000})
        # Incompressible data: ratio ~1 (below the default ratio cap), but the
        # total decompressed size exceeds the configured cap.
        data = _valid_capsule_zip(
            "total-run", {"blob.bin": _os.urandom(4096)}, stored=True
        )
        resp = _upload(client, data, "bomb-total.zip")
        _assert_guard(resp, "total_uncompressed")
        assert not (capsule_dir / "total-run").exists()

    def test_guards_zero_disables_each(
        self, make_client: Any, capsule_dir: Path
    ) -> None:
        client = make_client(
            {"zip_max_entries": 0, "zip_max_ratio": 0, "zip_max_uncompressed_bytes": 0}
        )
        members = {f"m{i}.bin": b"\x00" * (1024 * 1024) for i in range(3)}
        resp = _upload(client, _valid_capsule_zip("open-run", members))
        assert resp.status_code == 201, resp.text
        assert (capsule_dir / "open-run" / "m0.bin").exists()

    def test_streamed_guards_are_authoritative(self, tmp_path: Path) -> None:
        """The streaming pass re-measures totals independently of the central
        directory (which is attacker-controlled)."""
        from novafabric.server.errors import ValidationError
        from novafabric.server.ingest import extract_archive

        data = _zip_bytes({"a.bin": b"\x00" * 2048, "b.bin": b"\x00" * 2048})
        zf = zipfile.ZipFile(io.BytesIO(data))
        tmp_root = tmp_path / "x"
        tmp_root.mkdir()
        limits = IngestConfig(zip_max_uncompressed_bytes=1000, zip_max_ratio=0)
        with pytest.raises(ValidationError) as exc_info:
            extract_archive(zf, tmp_root, limits)
        assert exc_info.value.details is not None
        assert exc_info.value.details["reason"] == "total_uncompressed"

    def test_streamed_entry_count_guard(self, tmp_path: Path) -> None:
        from novafabric.server.errors import ValidationError
        from novafabric.server.ingest import extract_archive

        data = _zip_bytes({"a": b"1", "b": b"2", "c": b"3"})
        zf = zipfile.ZipFile(io.BytesIO(data))
        tmp_root = tmp_path / "y"
        tmp_root.mkdir()
        with pytest.raises(ValidationError) as exc_info:
            extract_archive(zf, tmp_root, IngestConfig(zip_max_entries=2))
        assert exc_info.value.details is not None
        assert exc_info.value.details["reason"] == "entry_count"


# --------------------------------------------------------------------------- #
# Config: defaults + env overrides (ADR-0029 conventions)
# --------------------------------------------------------------------------- #


class TestIngestConfig:
    def test_spec_defaults(self) -> None:
        cfg = IngestConfig()
        assert cfg.max_upload_bytes == 268_435_456  # 256 MiB
        assert cfg.spool_chunk_bytes == 1_048_576  # 1 MiB
        assert cfg.zip_max_entries == 10_000
        assert cfg.zip_max_uncompressed_bytes == 2_147_483_648  # 2 GiB
        assert cfg.zip_max_ratio == 100.0

    def test_absent_block_means_defaults(self) -> None:
        cfg = ServerConfig()
        assert cfg.ingest.max_upload_bytes == 268_435_456

    def test_yaml_block_overrides(self) -> None:
        cfg = ServerConfig.model_validate({"ingest": {"max_upload_bytes": 42}})
        assert cfg.ingest.max_upload_bytes == 42
        assert cfg.ingest.zip_max_entries == 10_000  # untouched siblings

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_INGEST_MAX_UPLOAD_BYTES", "1234")
        monkeypatch.setenv("NOVAFABRIC_SERVER_INGEST_SPOOL_CHUNK_BYTES", "131072")
        monkeypatch.setenv("NOVAFABRIC_SERVER_INGEST_ZIP_MAX_ENTRIES", "7")
        monkeypatch.setenv(
            "NOVAFABRIC_SERVER_INGEST_ZIP_MAX_UNCOMPRESSED_BYTES", "5555"
        )
        monkeypatch.setenv("NOVAFABRIC_SERVER_INGEST_ZIP_MAX_RATIO", "12.5")
        cfg = ServerConfig()
        assert cfg.ingest.max_upload_bytes == 1234
        assert cfg.ingest.spool_chunk_bytes == 131_072
        assert cfg.ingest.zip_max_entries == 7
        assert cfg.ingest.zip_max_uncompressed_bytes == 5555
        assert cfg.ingest.zip_max_ratio == 12.5

    def test_spool_chunk_floor_enforced(self) -> None:
        with pytest.raises(Exception):
            IngestConfig(spool_chunk_bytes=1024)  # must be >= 65536


# --------------------------------------------------------------------------- #
# Happy-path regression pins (response shape + pre-existing error codes)
# --------------------------------------------------------------------------- #


class TestHappyPathRegression:
    def test_upload_response_shape_unchanged(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        # Single-subdir archive: top-level component stripping is retained.
        data = _zip_bytes(
            {
                "mycapsule/capsule.yaml": yaml.safe_dump(_manifest("shape-run")).encode(),
                "mycapsule/trace.jsonl": b'{"a": 1}\n',
                "mycapsule/outputs/result.txt": b"ok",
            }
        )
        resp = _upload(client, data, "valid-small.zip")
        assert resp.status_code == 201, resp.text
        assert resp.json() == {
            "run_id": "shape-run",
            "status": "success",
            "created_at": "2026-04-15T10:00:00+00:00",
            "finished_at": "2026-04-15T10:00:01+00:00",
            "duration_ms": 1000,
            "command": ["python", "-c", "print('hi')"],
            "exit_code": 0,
        }
        dest = capsule_dir / "shape-run"
        assert (dest / "capsule.yaml").exists()
        assert (dest / "trace.jsonl").read_bytes() == b'{"a": 1}\n'
        assert (dest / "outputs" / "result.txt").read_bytes() == b"ok"

    def test_duplicate_upload_409_conflict(self, client: TestClient) -> None:
        data = _valid_capsule_zip("dup-run")
        assert _upload(client, data).status_code == 201
        resp = _upload(client, _valid_capsule_zip("dup-run"))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict"

    def test_child_with_missing_parent_409_parent_not_found(
        self, client: TestClient
    ) -> None:
        data = _valid_capsule_zip(
            "child-run",
            manifest_extra={
                "parent_run_id": "no-such-parent",
                "created_at": "2099-01-01T00:00:00+00:00",  # fresh → within window
            },
        )
        resp = _upload(client, data)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "parent_not_found"

    def test_empty_upload_400(self, client: TestClient) -> None:
        resp = _upload(client, b"")
        assert resp.status_code == 400
        assert "empty" in resp.json()["error"]["message"].lower()

    def test_not_a_zip_400(self, client: TestClient) -> None:
        resp = _upload(client, b"definitely not a zip archive")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "bad_request"

    def test_missing_manifest_400(self, client: TestClient) -> None:
        resp = _upload(client, _zip_bytes({"trace.jsonl": b""}))
        assert resp.status_code == 400
        assert "capsule.yaml" in resp.json()["error"]["message"]

    def test_invalid_manifest_yaml_400(self, client: TestClient) -> None:
        resp = _upload(client, _zip_bytes({"capsule.yaml": b"{unclosed: ["}))
        assert resp.status_code == 400
        assert "invalid YAML" in resp.json()["error"]["message"]

    def test_manifest_missing_run_id_400(self, client: TestClient) -> None:
        resp = _upload(client, _zip_bytes({"capsule.yaml": b"status: success\n"}))
        assert resp.status_code == 400
        assert "run_id" in resp.json()["error"]["message"]

    def test_directory_entries_are_skipped(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("capsule.yaml", yaml.safe_dump(_manifest("dir-run")))
            zf.writestr("outputs/", "")  # explicit directory entry
            zf.writestr("outputs/a.txt", "hi")
        resp = _upload(client, buf.getvalue())
        assert resp.status_code == 201, resp.text
        # This archive is FLAT (``capsule.yaml`` sits at the root), so no
        # component may be dropped and ``outputs/`` must survive.
        # Until ADR-0260 this asserted ``dir-run/a.txt`` — i.e. it pinned the
        # B12 flattening defect in place. See TestNestedMemberPreservation.
        assert (capsule_dir / "dir-run" / "outputs" / "a.txt").read_text() == "hi"
        assert not (capsule_dir / "dir-run" / "a.txt").exists()

    def test_concurrent_duplicate_racing_publish_409(
        self,
        client: TestClient,
        capsule_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TOCTOU narrowing: if the same run_id is published while this
        request is extracting, the pre-rename re-check 409s and cleans up."""
        from novafabric.server import ingest as ingest_mod

        real_extract = ingest_mod.extract_archive

        def racing_extract(zf: Any, tmp_root: Path, limits: Any) -> None:
            real_extract(zf, tmp_root, limits)
            # A concurrent request wins the race after our extraction.
            rival = capsule_dir / "race-run"
            rival.mkdir()
            (rival / "capsule.yaml").write_text("run_id: race-run\n")

        monkeypatch.setattr(ingest_mod, "extract_archive", racing_extract)
        resp = _upload(client, _valid_capsule_zip("race-run"))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict"
        # The rival's capsule is intact; our temp dir is gone.
        spool_dir = capsule_dir / ".ingest-tmp"
        assert list(spool_dir.iterdir()) == []
        assert (capsule_dir / "race-run" / "capsule.yaml").exists()


# --------------------------------------------------------------------------- #
# Unit coverage for the ingest helper branches
# --------------------------------------------------------------------------- #


class TestIngestHelpers:
    def test_unparseable_content_length_falls_through(self) -> None:
        from novafabric.server.ingest import check_content_length

        # Not a number → fast path defers to the counted stream (no raise).
        check_content_length("not-a-number", IngestConfig(max_upload_bytes=10))

    def test_read_member_guarded_trips_member_ratio(self) -> None:
        from novafabric.server.errors import ValidationError
        from novafabric.server.ingest import read_member_guarded

        data = _zip_bytes({"big.yaml": b"\x00" * (3 * 1024 * 1024)})
        zf = zipfile.ZipFile(io.BytesIO(data))
        with pytest.raises(ValidationError) as exc_info:
            read_member_guarded(zf, "big.yaml", IngestConfig(zip_max_ratio=5))
        assert exc_info.value.details is not None
        assert exc_info.value.details["reason"] == "compression_ratio"
        assert exc_info.value.details["member"] == "big.yaml"

    def test_archive_total_cap_across_members(self, tmp_path: Path) -> None:
        """Each member is under the cap, but the running archive total is not."""
        import os as _os

        from novafabric.server.errors import ValidationError
        from novafabric.server.ingest import extract_archive

        data = _zip_bytes(
            {"a.bin": _os.urandom(800), "b.bin": _os.urandom(800)}, stored=True
        )
        zf = zipfile.ZipFile(io.BytesIO(data))
        tmp_root = tmp_path / "z"
        tmp_root.mkdir()
        limits = IngestConfig(zip_max_uncompressed_bytes=1000, zip_max_ratio=0)
        with pytest.raises(ValidationError) as exc_info:
            extract_archive(zf, tmp_root, limits)
        assert exc_info.value.details is not None
        assert exc_info.value.details["reason"] == "total_uncompressed"
        assert exc_info.value.details["observed"] == 1600

    def test_safe_member_relpath_edge_names(self) -> None:
        from novafabric.server.errors import ValidationError
        from novafabric.server.ingest import safe_member_relpath

        assert safe_member_relpath("dir/") is None  # directory entry
        assert safe_member_relpath(".") is None  # degenerate name
        # Absolute names lose the root, then the top-level component
        # (historical, incidental neutering — now explicit).
        assert safe_member_relpath("/etc/passwd") == Path("passwd")
        assert safe_member_relpath("flat.txt") == Path("flat.txt")
        assert safe_member_relpath("top/nested/x.txt") == Path("nested/x.txt")
        with pytest.raises(ValidationError):
            safe_member_relpath("a/../../evil")


class TestNestedMemberPreservation:
    """B12 — ingest must not flatten nested capsule files.

    Found on live infrastructure: a real capsule uploaded through REST arrived
    with ``outputs/stdout.txt`` written to ``stdout.txt``, overwriting the
    sibling ``inputs/stdout.txt`` that flattened onto the same name. The server
    returned 201 and the capsule verified 12 of 14 digests, so nothing in the
    response or the store reported the loss.

    The strip is only correct when the archive has a single top-level directory
    (``<run_id>/...``). It must be a whole-archive decision, not a per-member one.
    """

    def test_flat_archive_keeps_nested_paths(self, tmp_path: Path) -> None:
        """AC1/AC3 — the exact B12 reproduction: 4 members in, 4 files out."""
        from novafabric.server.ingest import extract_archive

        data = _zip_bytes(
            {
                "capsule.yaml": b"manifest",
                "outputs/stdout.txt": b"I AM OUTPUTS",
                "inputs/stdout.txt": b"I AM INPUTS",
                "trace.jsonl": b"{}",
            }
        )
        zf = zipfile.ZipFile(io.BytesIO(data))
        root = tmp_path / "x"
        root.mkdir()
        extract_archive(zf, root, IngestConfig())

        written = sorted(
            str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
        )
        assert written == [
            "capsule.yaml",
            "inputs/stdout.txt",
            "outputs/stdout.txt",
            "trace.jsonl",
        ]
        # the overwrite is the damaging half of the defect
        assert (root / "outputs/stdout.txt").read_bytes() == b"I AM OUTPUTS"
        assert (root / "inputs/stdout.txt").read_bytes() == b"I AM INPUTS"

    def test_single_rooted_archive_still_strips_its_root(
        self, tmp_path: Path
    ) -> None:
        """AC2 — the legitimate case keeps working, nested structure intact."""
        from novafabric.server.ingest import extract_archive

        data = _zip_bytes(
            {
                "01ABC/capsule.yaml": b"manifest",
                "01ABC/outputs/stdout.txt": b"out",
                "01ABC/inputs/stdout.txt": b"in",
            }
        )
        zf = zipfile.ZipFile(io.BytesIO(data))
        root = tmp_path / "x"
        root.mkdir()
        extract_archive(zf, root, IngestConfig())

        written = sorted(
            str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
        )
        assert written == ["capsule.yaml", "inputs/stdout.txt", "outputs/stdout.txt"]
        assert (root / "outputs/stdout.txt").read_bytes() == b"out"

    def test_mixed_roots_strip_nothing(self, tmp_path: Path) -> None:
        """AC5 — no single shared root means no component may be dropped."""
        from novafabric.server.ingest import extract_archive

        data = _zip_bytes({"a/x.txt": b"1", "b/y.txt": b"2"})
        zf = zipfile.ZipFile(io.BytesIO(data))
        root = tmp_path / "x"
        root.mkdir()
        extract_archive(zf, root, IngestConfig())

        written = sorted(
            str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
        )
        assert written == ["a/x.txt", "b/y.txt"]


class TestOrphanedSpoolReclamation:
    """B9 — a spool file orphaned by a crash must not survive forever.

    Measured on the live hub: an upload in flight at 16:18:57 was interrupted
    when the server was SIGKILLed; the server restarted 4 s later and the
    6,882-byte `.spool` file was still present 52 minutes and one full service
    lifecycle afterwards, while the store published 4,826 capsules around it.
    There is no reaper on startup, on a timer, or on the ingest path.
    """

    def test_reaper_removes_spool_files_older_than_process_start(
        self, tmp_path: Path
    ) -> None:
        import os
        import time

        from novafabric.server.ingest import SPOOL_DIR_NAME, reap_orphaned_spools

        spool_dir = tmp_path / SPOOL_DIR_NAME
        spool_dir.mkdir()
        orphan = spool_dir / "tmpdead.spool"
        orphan.write_bytes(b"x" * 10)
        old = time.time() - 3600
        os.utime(orphan, (old, old))

        removed = reap_orphaned_spools(spool_dir, started_at=time.time())
        assert removed == 1
        assert not orphan.exists()

    def test_reaper_never_touches_an_in_flight_spool(self, tmp_path: Path) -> None:
        """AC2 — a spool newer than process start belongs to a live request."""
        import time

        from novafabric.server.ingest import SPOOL_DIR_NAME, reap_orphaned_spools

        spool_dir = tmp_path / SPOOL_DIR_NAME
        spool_dir.mkdir()
        started_at = time.time() - 60  # process started a minute ago
        live = spool_dir / "tmplive.spool"
        live.write_bytes(b"in flight")  # mtime = now, i.e. after startup

        assert reap_orphaned_spools(spool_dir, started_at=started_at) == 0
        assert live.exists()

    def test_reaper_ignores_non_spool_files(self, tmp_path: Path) -> None:
        import os
        import time

        from novafabric.server.ingest import SPOOL_DIR_NAME, reap_orphaned_spools

        spool_dir = tmp_path / SPOOL_DIR_NAME
        spool_dir.mkdir()
        keep = spool_dir / "notes.txt"
        keep.write_text("keep me")
        old = time.time() - 3600
        os.utime(keep, (old, old))

        assert reap_orphaned_spools(spool_dir, started_at=time.time()) == 0
        assert keep.exists()

    def test_reaper_is_silent_when_the_directory_is_absent(
        self, tmp_path: Path
    ) -> None:
        """AC3 — a missing or unreadable spool dir must never block startup."""
        import time

        from novafabric.server.ingest import reap_orphaned_spools

        assert reap_orphaned_spools(tmp_path / "nope", started_at=time.time()) == 0

    def test_server_startup_actually_reaps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reaper must be WIRED, not merely defined.

        An unexecuted reclamation path is the same defect class as the leak it
        is meant to fix, so this drives the app's real lifespan rather than
        calling the function directly.
        """
        import os
        import time

        from novafabric.server.ingest import SPOOL_DIR_NAME

        store = tmp_path / "caps"
        spool_dir = store / SPOOL_DIR_NAME
        spool_dir.mkdir(parents=True)
        orphan = spool_dir / "tmporphan.spool"
        orphan.write_bytes(b"stranded by a crash")
        old = time.time() - 3600
        os.utime(orphan, (old, old))
        monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(store))

        cfg = ServerConfig.model_validate(
            {"db_path": str(tmp_path / "t.db"), "insecure_no_auth": True}
        )
        with TestClient(create_app(cfg), raise_server_exceptions=False):
            pass  # entering the context runs the lifespan startup

        assert not orphan.exists(), "startup did not reclaim the orphaned spool"
