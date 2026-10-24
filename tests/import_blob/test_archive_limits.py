"""``nova import`` bounds what an untrusted archive may expand to (review storage-backup-15).

The courier archive was read whole into RAM, each member was read whole into RAM,
and nothing capped the entry count, the uncompressed size or the compression ratio:
a small ``.tar.gz`` that expands to tens of gigabytes filled the temp disk or
OOM-killed the CLI. The server ingest path had exactly these caps; import now
applies the same defaults, and streams members to disk.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from novafabric.import_blob import unpack
from novafabric.import_blob.unpack import UnpackError, safe_extract_tar


def _tar(members: list[tuple[str, bytes]], *, gz: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_the_defaults_match_server_ingest() -> None:
    from novafabric.server.config import IngestConfig

    ingest = IngestConfig()
    limits = unpack.DEFAULT_LIMITS
    assert limits.max_entries == ingest.zip_max_entries
    assert limits.max_uncompressed_bytes == ingest.zip_max_uncompressed_bytes
    assert limits.max_ratio == ingest.zip_max_ratio


def test_too_many_entries_is_refused(tmp_path: Path) -> None:
    data = _tar([(f"f{i}", b"x") for i in range(5)])
    limits = unpack.ExtractLimits(max_entries=4)
    with pytest.raises(UnpackError, match="entries"):
        safe_extract_tar(data, tmp_path / "out", limits=limits)


def test_too_many_uncompressed_bytes_is_refused_before_writing(tmp_path: Path) -> None:
    data = _tar([("a", b"x" * 600), ("b", b"y" * 600)])
    limits = unpack.ExtractLimits(max_uncompressed_bytes=1000)
    with pytest.raises(UnpackError, match="uncompressed"):
        safe_extract_tar(data, tmp_path / "out", limits=limits)
    assert not (tmp_path / "out" / "b").exists()


def test_a_compression_bomb_is_refused(tmp_path: Path) -> None:
    data = _tar([("zeros", bytes(4 * 1024 * 1024))], gz=True)  # ~4 KiB compressed
    with pytest.raises(UnpackError, match="ratio"):
        safe_extract_tar(data, tmp_path / "out")


def test_an_ordinary_archive_extracts_from_a_path_by_streaming(tmp_path: Path) -> None:
    archive = tmp_path / "in.tar.gz"
    archive.write_bytes(_tar([("dir/a.txt", b"hello"), ("b.txt", b"world" * 1000)], gz=True))
    assert safe_extract_tar(archive, tmp_path / "out") == 2
    assert (tmp_path / "out" / "dir" / "a.txt").read_bytes() == b"hello"
    assert (tmp_path / "out" / "b.txt").read_bytes() == b"world" * 1000
