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
"""Confine filesystem paths taken from a ``nova serve`` request to allowlisted roots.

Several dashboard endpoints accept a server-side filesystem path in the request
body: an evidence-export ``output_path``/``key_path``, a policy-sign
``key_path``/``cert_path``/``db_path``, a capsule-migrate ``source``/``output``,
and others. The guard used to be a *denylist* of system directories (``/etc``,
``/usr``, ...). That left everything else open, including the home directory of
the server user and its ``~/.ssh``, so an ``operate``-scoped token could read
or overwrite arbitrary files as the server user.

This module is an **allowlist**. A path is accepted only if, after ``~``
expansion and full resolution (``..`` and every symlink followed), it lies
inside one of a fixed set of roots:

* **write roots**: ``$NOVAFABRIC_HOME/exports``, ``$NOVAFABRIC_HOME/evidence``
  and the configured evidence directory. They are deliberately *not* all of
  ``$NOVAFABRIC_HOME``, which also holds ``.serve-token``, ``tokens.jsonl``,
  the audit log and the registry: a write root there would let a caller
  overwrite those files.
* **read roots**: ``$NOVAFABRIC_HOME``, the server's capsule directory, and the
  write roots.
* both lists also take every absolute directory an operator lists in
  ``NOVAFABRIC_SERVE_PATH_ROOTS`` (``os.pathsep``-separated). Only the server's
  environment sets this; no request can.

A relative path is taken relative to the first root of its list
(``$NOVAFABRIC_HOME`` for reads, ``$NOVAFABRIC_HOME/exports`` for writes). Any
``..`` segment is refused outright, even one that would resolve back inside a
root. Both rules keep the check simple to reason about.

**Limit:** the check resolves the path when the request arrives. Someone who can
already create symlinks *inside* a root can swap one between that check and the
later read or write. No endpoint lets a request create a symlink, so this
requires local access to the machine.

The roots are computed per call from the environment, so there is no module
state and no import-time IO.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from novafabric import _paths as _nf_paths

__all__ = [
    "LEGACY_ALLOW_ANY_ENV",
    "PATH_ROOTS_ENV",
    "InvalidRequestPathError",
    "PathConfinementError",
    "PathOutsideRootsError",
    "confine",
    "extra_roots",
    "read_roots",
    "write_roots",
]

#: Operator-configured extra roots (read and write), ``os.pathsep``-separated.
PATH_ROOTS_ENV: Final[str] = "NOVAFABRIC_SERVE_PATH_ROOTS"

#: The denylist's opt-out. It is **no longer honoured**. It is named here so the
#: server can warn an operator who still sets it.
LEGACY_ALLOW_ANY_ENV: Final[str] = "NOVAFABRIC_SERVE_ALLOW_ANY_PATH"


class PathConfinementError(ValueError):
    """A request-supplied path was refused. ``status_code`` is the HTTP answer."""

    status_code: int = 403


class InvalidRequestPathError(PathConfinementError):
    """The path is empty or not a usable path (for example, it holds a NUL byte)."""

    status_code = 400


class PathOutsideRootsError(PathConfinementError):
    """The path has a ``..`` segment or resolves outside every allowed root."""

    status_code = 403


def _resolved_unique(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: list[Path] = []
    for p in paths:
        r = p.expanduser().resolve()
        if r not in seen:
            seen.append(r)
    return tuple(seen)


def extra_roots() -> tuple[Path, ...]:
    """Absolute directories listed in :data:`PATH_ROOTS_ENV`. Relative entries are ignored."""
    raw = os.environ.get(PATH_ROOTS_ENV, "")
    out: list[Path] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        p = Path(entry).expanduser()
        if p.is_absolute():
            out.append(p)
    return tuple(out)


def write_roots() -> tuple[Path, ...]:
    """Directories a request may name as a write target, resolved; first is the default base."""
    home = _nf_paths.nova_home()
    return _resolved_unique(
        [home / "exports", home / "evidence", _nf_paths.evidence_dir(), *extra_roots()]
    )


def read_roots(capsule_dir: Path | None = None) -> tuple[Path, ...]:
    """Directories a request may name as a read source, resolved; first is the default base."""
    base: list[Path] = [_nf_paths.nova_home()]
    if capsule_dir is not None:
        base.append(capsule_dir)
    return _resolved_unique([*base, *write_roots(), *extra_roots()])


def confine(candidate: str | os.PathLike[str], roots: Sequence[Path]) -> Path:
    """Resolve *candidate* and return it only if it lies inside one of *roots*.

    *roots* must already be resolved, as :func:`read_roots` and
    :func:`write_roots` return them. The returned path is fully resolved; use it
    rather than the raw input, so the path that was checked is the path used.

    Raises:
        InvalidRequestPathError: empty, or not representable as a path.
        PathOutsideRootsError: a ``..`` segment, or the resolved path (symlinks
            followed) is outside every root.
    """
    if not roots:
        raise PathOutsideRootsError("no server-side path roots are configured")
    try:
        raw = os.fspath(candidate)
    except TypeError as exc:
        raise InvalidRequestPathError("path must be a string") from exc
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidRequestPathError("path must be a non-empty string")
    if "\x00" in raw:
        raise InvalidRequestPathError("path contains a NUL byte")

    path = Path(raw).expanduser()
    if ".." in path.parts:
        raise PathOutsideRootsError("path may not contain '..' segments")
    if not path.is_absolute():
        path = roots[0] / path
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise InvalidRequestPathError(f"path cannot be resolved: {exc}") from exc

    for root in roots:
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    raise PathOutsideRootsError(
        "path resolves outside the directories this server may use "
        f"({', '.join(str(r) for r in roots)}); an operator can add a directory "
        f"with {PATH_ROOTS_ENV}"
    )
