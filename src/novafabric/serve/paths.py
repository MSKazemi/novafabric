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

Read roots include all of ``$NOVAFABRIC_HOME``, and that home also holds the
server's own secrets. So a read is additionally checked against a **deny list**
(:func:`secret_paths`): the token stores (``tokens.jsonl``, ``.serve-token``,
``.server-token``), ``.env``, the PII data-encryption-key store (``dek.db`` and
its SQLite sidecars), the NovaSeal ratchet state, ``config.yaml``, and the
audit logs. A path is refused if it *is* one of those, lies inside one, or is a
directory that *contains* one: ``$NOVAFABRIC_HOME`` itself cannot be named as a
policy bundle or a capsule base, for example. A source read (:func:`confine_read`)
also refuses key material: the ``keys/`` store, the NovaSeal keyring and any
``*.pem`` or ``*.key`` file. A signing-key read (:func:`confine_key`) may name
key material, which the server uses but never returns, and nothing else on the
deny list.

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
    "KEY_SUFFIXES",
    "LEGACY_ALLOW_ANY_ENV",
    "PATH_ROOTS_ENV",
    "InvalidRequestPathError",
    "PathConfinementError",
    "PathDeniedError",
    "PathOutsideRootsError",
    "confine",
    "confine_key",
    "confine_read",
    "extra_roots",
    "key_material_paths",
    "read_roots",
    "secret_paths",
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


class PathDeniedError(PathConfinementError):
    """The path is inside a root but names, holds or contains server secrets."""

    status_code = 403


#: File-name suffixes treated as key material wherever they appear.
KEY_SUFFIXES: Final[tuple[str, ...]] = (".pem", ".key")

_SQLITE_SIDECARS: Final[tuple[str, ...]] = ("", "-wal", "-shm", "-journal")


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


def secret_paths() -> tuple[Path, ...]:
    """Server secrets no request may name as a source, resolved.

    Found through :mod:`novafabric._paths` and the stores' own helpers, so an
    override env var (``NOVAFABRIC_RATCHET_DIR``, ``NOVAFABRIC_AUDIT_LOG_PATH``, ...)
    moves the entry with the store.
    """
    from novafabric.audit import resolve_audit_log_path
    from novafabric.trust.novaseal.ratchet import ratchet_dir

    home = _nf_paths.nova_home()
    return _resolved_unique(
        [
            _nf_paths.tokens_path(),
            _nf_paths.serve_token_path(),
            _nf_paths.server_token_path(),
            home / ".env",
            *(home / f"dek.db{suffix}" for suffix in _SQLITE_SIDECARS),
            ratchet_dir(),
            home / "config.yaml",
            _nf_paths.dashboard_audit_path(),
            resolve_audit_log_path(),
        ]
    )


def key_material_paths() -> tuple[Path, ...]:
    """Private-key stores, resolved: ``$NOVAFABRIC_HOME/keys`` and the NovaSeal keyring."""
    from novafabric.trust.keyring import _KEYRING_DIR

    return _resolved_unique([_nf_paths.keys_dir(), _KEYRING_DIR])


def _denied(resolved: Path, deny: Sequence[Path], deny_suffixes: Sequence[str]) -> str | None:
    for secret in deny:
        if resolved == secret or resolved.is_relative_to(secret):
            return f"{secret.name} holds server secrets"
        if secret.is_relative_to(resolved):
            return f"it contains {secret.name}, which holds server secrets"
    if resolved.name.endswith(tuple(deny_suffixes)):
        return "it is key material"
    return None


def confine(
    candidate: str | os.PathLike[str],
    roots: Sequence[Path],
    *,
    deny: Sequence[Path] = (),
    deny_suffixes: Sequence[str] = (),
) -> Path:
    """Resolve *candidate* and return it only if it lies inside one of *roots*.

    *roots* must already be resolved, as :func:`read_roots` and
    :func:`write_roots` return them. The returned path is fully resolved; use it
    rather than the raw input, so the path that was checked is the path used.

    *deny* (resolved paths) and *deny_suffixes* name what may not be reached
    even inside a root: the resolved path may not equal, lie inside, or contain
    any *deny* entry, nor end in any *deny_suffixes*.

    Raises:
        InvalidRequestPathError: empty, or not representable as a path.
        PathOutsideRootsError: a ``..`` segment, or the resolved path (symlinks
            followed) is outside every root.
        PathDeniedError: the resolved path hits the deny list.
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
            reason = _denied(resolved, deny, deny_suffixes)
            if reason is not None:
                raise PathDeniedError(f"path refused: {reason}")
            return resolved
    raise PathOutsideRootsError(
        "path resolves outside the directories this server may use "
        f"({', '.join(str(r) for r in roots)}); an operator can add a directory "
        f"with {PATH_ROOTS_ENV}"
    )


def confine_read(candidate: str | os.PathLike[str], capsule_dir: Path | None = None) -> Path:
    """Confine a path a request names as a *source*: read roots minus every secret.

    Refuses the :func:`secret_paths`, the :func:`key_material_paths` and any
    ``*.pem``/``*.key`` file, and any directory that contains one of them.
    """
    return confine(
        candidate,
        read_roots(capsule_dir),
        deny=(*secret_paths(), *key_material_paths()),
        deny_suffixes=KEY_SUFFIXES,
    )


def confine_key(candidate: str | os.PathLike[str], capsule_dir: Path | None = None) -> Path:
    """Confine a path a request names as a *signing key or certificate*.

    Key material is allowed: the server reads it to sign and never returns it.
    The other :func:`secret_paths` (token stores, the DEK store, the ratchet
    state, ``config.yaml``, the audit logs) are still refused.
    """
    return confine(candidate, read_roots(capsule_dir), deny=secret_paths())
