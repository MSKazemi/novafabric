"""``nova restore`` never creates a link and never writes through one.

``create_backup`` now refuses a home whose capsules hold a symlink (see
``tests/capsule/test_capsule_links_never_followed.py``). This file covers the
other direction. A crafted set whose manifest lists a *link* member is refused
before the home is touched: tarfile would hand the link target's bytes back
from ``extractfile``, so the digest check alone does not catch it. A normal
round trip leaves no link anywhere in the restored home, and the writer opens
each destination with ``O_NOFOLLOW``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

from novafabric.backup import RestoreError, create_backup, restore_backup
from novafabric.backup.models import BackupManifest, BackupMember

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")


@pytest.fixture(autouse=True)
def _no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)


def _set_with_link_member(path: Path, *, linkname: str, link_type: bytes) -> Path:
    """A set listing ``capsules/r/inputs/x``, stored as a link to ``capsules/r/capsule.yaml``."""
    body = b"run_id: r\n"
    digest = hashlib.sha256(body).hexdigest()
    manifest = BackupManifest(
        set_id="01HZZZZZZZZZZZZZZZZZZZZZZZ",
        created_at="2026-10-22T00:00:00+00:00",
        nova_version="0.0.0-test",
        members=[
            BackupMember(
                path="capsules/r/capsule.yaml", sha256=digest, size_bytes=len(body), kind="blob"
            ),
            BackupMember(
                path="capsules/r/inputs/x", sha256=digest, size_bytes=len(body), kind="blob"
            ),
        ],
    )
    raw = json.dumps(manifest.model_dump(mode="json")).encode()
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (("manifest.json", raw), ("capsules/r/capsule.yaml", body)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("capsules/r/inputs/x")
        link.type = link_type
        link.linkname = linkname
        tar.addfile(link)
    return path


@pytest.mark.parametrize(
    ("linkname", "link_type"),
    [
        ("capsules/r/capsule.yaml", tarfile.SYMTYPE),
        ("../capsule.yaml", tarfile.SYMTYPE),
        ("capsules/r/capsule.yaml", tarfile.LNKTYPE),
    ],
)
def test_restore_refuses_a_link_member_before_touching_the_home(
    tmp_path: Path, linkname: str, link_type: bytes
) -> None:
    raw = _set_with_link_member(tmp_path / "evil.tar.gz", linkname=linkname, link_type=link_type)
    home = tmp_path / "home"
    # A relative link to a name the archive does not hold already fails the
    # digest check; one that aliases a real member only fails the type check.
    with pytest.raises(RestoreError, match="not a regular file|failed verification"):
        restore_backup(raw, home=home)
    assert not home.exists()


def test_round_trip_restores_no_links(tmp_path: Path) -> None:
    from novafabric.registry.store import get_connection, init_schema

    src = tmp_path / "src"
    (src / "capsules" / "r1" / "inputs").mkdir(parents=True)
    conn = get_connection(src / "registry.db")
    init_schema(conn)
    conn.close()
    (src / "capsules" / "r1" / "capsule.yaml").write_text("run_id: r1\n")
    (src / "capsules" / "r1" / "inputs" / "a.txt").write_text("a\n")
    archive = create_backup(tmp_path / "set.tar.gz", home=src).archive_path

    home = tmp_path / "restored"
    restore_backup(archive, home=home)
    assert (home / "capsules" / "r1" / "inputs" / "a.txt").read_text() == "a\n"
    links = [
        os.path.join(d, n)
        for d, ds, fs in os.walk(home, followlinks=False)
        for n in ds + fs
        if os.path.islink(os.path.join(d, n))
    ]
    assert links == []


def test_write_member_never_writes_through_a_link(tmp_path: Path) -> None:
    from novafabric.backup.restore import _write_member_bytes

    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n")
    dst = tmp_path / "home" / "x"
    dst.parent.mkdir()
    os.symlink(victim, dst)
    with pytest.raises(RestoreError, match="symlink"):
        _write_member_bytes(io.BytesIO(b"payload"), dst)
    assert victim.read_text() == "untouched\n"
