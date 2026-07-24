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

"""Hardened tar extraction for untrusted import archives (ADR-0207 D3).

An import consumes archives produced elsewhere — untrusted input by definition
(ADR-0009). The deterministic packer only ever emits relative, sorted, regular
file/dir entries, but the importer MUST NOT trust the packer: every member name
is sanitized (no absolute paths, no ``..`` components, no drive/UNC prefixes)
and only regular files and directories are extracted — links, devices, and
FIFOs are rejected outright.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path, PurePosixPath


class UnpackError(Exception):
    """The archive (or one of its members) is unsafe or unreadable."""


def _validate_member_name(name: str) -> PurePosixPath:
    """Return the sanitized relative path for *name*, or raise :class:`UnpackError`."""
    pure = PurePosixPath(name)
    if pure.is_absolute() or name.startswith(("/", "\\")):
        raise UnpackError(f"unsafe member name (absolute path): {name!r}")
    parts = pure.parts
    if not parts:
        raise UnpackError("unsafe member name (empty)")
    if any(part == ".." for part in parts):
        raise UnpackError(f"unsafe member name ('..' component): {name!r}")
    if any(("\\" in part or ":" in part) for part in parts):
        # Windows-style separators / drive prefixes never appear in honest
        # exports; refuse rather than guess.
        raise UnpackError(f"unsafe member name (suspicious component): {name!r}")
    return pure


def safe_extract_tar(data: bytes, target: Path) -> int:
    """Extract tar *data* into *target* with a sanitizing walk; return file count.

    Only regular files and directories are written. Any unsafe or unsupported
    member refuses the **whole** archive (an archive that needs one unsafe
    member dropped is not an archive to trust), and *target* is left as-is —
    the caller stages into a scratch directory and discards it on error.
    """
    target.mkdir(parents=True, exist_ok=True)
    resolved_target = target.resolve()
    extracted = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for member in tf:
                rel = _validate_member_name(member.name)
                dest = resolved_target.joinpath(*rel.parts)
                # Belt-and-braces: the sanitized path must stay inside target.
                # Unreachable after _validate_member_name (no absolute names,
                # no '..', and we never create symlinks) — kept as defense.
                if not dest.resolve().is_relative_to(
                    resolved_target
                ):  # pragma: no cover
                    raise UnpackError(
                        f"member escapes extraction root: {member.name!r}"
                    )
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif member.isreg():
                    fileobj = tf.extractfile(member)
                    if fileobj is None:  # pragma: no cover — isreg implies a body
                        raise UnpackError(f"unreadable member: {member.name!r}")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(fileobj.read())
                    extracted += 1
                else:
                    raise UnpackError(
                        "unsupported member type (links/devices/FIFOs refused): "
                        f"{member.name!r}"
                    )
    except tarfile.TarError as exc:
        raise UnpackError(f"not a readable tar archive: {exc}") from exc
    return extracted
