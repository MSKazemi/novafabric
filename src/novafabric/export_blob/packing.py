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
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import yaml


class CapsulePackError(Exception):
    """The capsule directory cannot be packed for export."""


def capsule_identifier(capsule_dir: Path) -> str:
    """The capsule's id: ``run_id`` from ``capsule.yaml``, else the dir name."""
    capsule_yaml = capsule_dir / "capsule.yaml"
    if capsule_yaml.is_file():
        try:
            meta = yaml.safe_load(capsule_yaml.read_text())
        except yaml.YAMLError:
            meta = None
        if isinstance(meta, dict) and meta.get("run_id"):
            return str(meta["run_id"])
    return capsule_dir.name


def pack_capsule(capsule_dir: Path) -> bytes:
    """Deterministic tar bytes of *capsule_dir* (read-only over the source).

    Entries are sorted by relative POSIX path; mtime/uid/gid are zeroed,
    uname/gname emptied, modes normalized (0644 files, 0755 dirs). Symlinks are
    followed and stored as regular files so the archive is self-contained.
    """
    if not capsule_dir.is_dir():
        raise CapsulePackError(f"not a capsule directory: {capsule_dir}")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        entries = sorted(
            capsule_dir.rglob("*"), key=lambda p: p.relative_to(capsule_dir).as_posix()
        )
        for path in entries:
            rel = path.relative_to(capsule_dir).as_posix()
            info = tarfile.TarInfo(name=rel)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            elif path.is_file():
                data = path.read_bytes()
                info.size = len(data)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(data))
            # Sockets/FIFOs/broken symlinks are skipped: not capsule content.
    return buf.getvalue()
