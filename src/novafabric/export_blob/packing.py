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

"""Deterministic capsule packing for content-addressed export (ADR-0141 D2).

A run capsule is a directory; the exported object is a **deterministic,
uncompressed tar** of that directory (sorted entries, zeroed timestamps and
ownership, normalized modes). Determinism is what makes the export idempotent:
re-packing an unchanged capsule reproduces byte-identical output and therefore
the same CAS address, so the blob is skipped on re-run. Packing is strictly
read-only over the capsule directory — the source is never mutated.

A capsule that holds a symbolic link is refused (:class:`CapsulePackLinkError`):
packing used to follow links and store the *target* as a capsule file, so
``inputs/x -> ~/.ssh/id_rsa`` exported the key. Every read goes through
:mod:`novafabric._capsule_fs`, which never follows a link.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import yaml

from novafabric import _capsule_fs
from novafabric._capsule_fs import CapsuleLinkError, read_capsule_file, read_capsule_member_or_none

_OPERATION = "a blob export"


class CapsulePackError(Exception):
    """The capsule directory cannot be packed for export."""


class CapsulePackLinkError(CapsulePackError, CapsuleLinkError):
    """The capsule holds a symbolic link; packing never follows one."""


def capsule_identifier(capsule_dir: Path) -> str:
    """The capsule's id: ``run_id`` from ``capsule.yaml``, else the dir name."""
    capsule_yaml = capsule_dir / "capsule.yaml"
    if capsule_yaml.is_file():
        try:
            capsule_text = (
                read_capsule_member_or_none(capsule_yaml, operation="nova export-blob") or ""
            )
            meta = yaml.safe_load(capsule_text)
        except yaml.YAMLError:
            meta = None
        if isinstance(meta, dict) and meta.get("run_id"):
            return str(meta["run_id"])
    return capsule_dir.name


def pack_capsule(capsule_dir: Path) -> bytes:
    """Deterministic tar bytes of *capsule_dir* (read-only over the source).

    Entries are sorted by relative POSIX path; mtime/uid/gid are zeroed,
    uname/gname emptied, modes normalized (0644 files, 0755 dirs). Sockets and
    FIFOs are skipped: they are not capsule content.

    Raises:
        CapsulePackLinkError: the capsule holds a symlink, including one planted
            while it is being packed. Nothing is returned.
        CapsulePackError: *capsule_dir* is not a directory.
    """
    if not capsule_dir.is_dir():
        raise CapsulePackError(f"not a capsule directory: {capsule_dir}")

    listing = _capsule_fs.list_capsule(
        capsule_dir, operation=_OPERATION, error=CapsulePackLinkError
    )
    entries = sorted(
        [(rel, True) for rel in listing.dirs] + [(rel, False) for rel in listing.files]
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for rel, is_dir in entries:
            info = tarfile.TarInfo(name=rel)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if is_dir:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
                continue
            data = read_capsule_file(
                listing.root, rel, operation=_OPERATION, error=CapsulePackLinkError
            )
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()

