"""Seal-aware, symlink-refusing, atomic rewrite of a capsule's ``capsule.yaml``.

Several annotation commands (``nova consent record``, ``nova insurance … --write``,
``nova science receipt build --write``) merge a facet into an existing capsule's
manifest. Doing that naively has three failure modes this module closes:

1. **Seal breakage.** A NovaSeal seal (``<capsule>/.seal/``, see
   ``nova verify``) signs a payload that ``nova verify`` compares to the on-disk
   ``capsule.yaml``. Rewriting the manifest of a sealed capsule makes an honest
   annotation indistinguishable from tampering (binding check fails, CRITICAL),
   and nothing in these commands can re-seal it. A sealed capsule is therefore
   refused unless the caller passes ``force_unseal=True`` (the CLI flag
   :data:`FORCE_UNSEAL_FLAG`), in which case the write proceeds and the caller
   must warn loudly that the seal no longer verifies.
2. **Symlink redirection.** ``Path.write_text`` follows a symlinked
   ``capsule.yaml`` and overwrites the link target (e.g. a signing profile). A
   symlinked manifest or a symlinked capsule directory is refused.
3. **Torn writes.** The new manifest is written to a temp file in the same
   directory, fsynced, ``os.replace``-d over the old one, and the directory is
   fsynced; a crash leaves either the old or the new manifest, never a
   truncated one. The original file mode is preserved.

Unsealed, regular capsules behave exactly as before: the manifest is replaced.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from novafabric.capsule._atomic import atomic_replace

MANIFEST_NAME = "capsule.yaml"
#: Seal directory ``nova verify`` and the capture orchestrator use.
SEAL_DIR_NAME = ".seal"
#: The CLI flag every manifest-rewriting command exposes to override the seal refusal.
FORCE_UNSEAL_FLAG = "--force-unseal"
#: Help text shared by every command that exposes :data:`FORCE_UNSEAL_FLAG`.
FORCE_UNSEAL_HELP = (
    "Rewrite capsule.yaml even though the capsule is NovaSeal-sealed. The existing "
    "seal will no longer verify (nova verify reports a binding failure) and must be "
    "re-issued."
)
#: Warning a caller prints after a forced write over a sealed capsule.
UNSEAL_WARNING = (
    "WARNING: capsule.yaml of a SEALED capsule was rewritten ({flag}). The existing "
    "NovaSeal seal in .seal/ no longer matches and `nova verify` will now fail; "
    "re-issue the seal."
).format(flag=FORCE_UNSEAL_FLAG)
_DEFAULT_MODE = 0o644


class ManifestWriteError(Exception):
    """``capsule.yaml`` could not be rewritten safely; nothing on disk changed."""


class SealedCapsuleError(ManifestWriteError):
    """The capsule is sealed and the caller did not pass ``force_unseal``."""


class UnsafeManifestPathError(ManifestWriteError):
    """The manifest or its capsule directory is a symlink or not a regular path."""


@dataclass(frozen=True)
class ManifestWriteResult:
    """Outcome of :func:`write_capsule_manifest`.

    Attributes:
        path: The manifest that was replaced.
        was_sealed: True when a seal was present and ``force_unseal`` overrode it;
            the caller must then print :data:`UNSEAL_WARNING`.
    """

    path: Path
    was_sealed: bool


def is_sealed(capsule_dir: Path) -> bool:
    """True when ``capsule_dir`` carries a NovaSeal seal (``.seal/`` present).

    Mirrors ``nova verify``'s detection. ``lexists`` is used so a dangling or
    symlinked ``.seal`` still counts as sealed — fail closed.
    """
    return os.path.lexists(capsule_dir / SEAL_DIR_NAME)


def _check_paths(capsule_dir: Path, target: Path) -> int:
    """Refuse symlinked/non-regular paths; return the mode to give the new file."""
    try:
        dir_st = os.lstat(capsule_dir)
    except OSError as exc:
        raise UnsafeManifestPathError(
            f"cannot stat capsule directory: {type(exc).__name__}"
        ) from exc
    if stat.S_ISLNK(dir_st.st_mode):
        raise UnsafeManifestPathError("capsule directory is a symlink; refusing to write")
    if not stat.S_ISDIR(dir_st.st_mode):
        raise UnsafeManifestPathError("capsule path is not a directory; refusing to write")
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return _DEFAULT_MODE
    except OSError as exc:
        raise UnsafeManifestPathError(f"cannot stat {MANIFEST_NAME}: {type(exc).__name__}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeManifestPathError(f"{MANIFEST_NAME} is a symlink; refusing to replace it")
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeManifestPathError(f"{MANIFEST_NAME} is not a regular file; refusing")
    return stat.S_IMODE(st.st_mode)


def write_capsule_manifest(
    capsule_dir: Path, text: str, *, force_unseal: bool = False
) -> ManifestWriteResult:
    """Atomically replace ``<capsule_dir>/capsule.yaml`` with ``text``.

    Args:
        capsule_dir: The capsule directory (must not be a symlink).
        text: The full new manifest content (already serialised YAML).
        force_unseal: Write even when the capsule is sealed. The caller must
            then surface :data:`UNSEAL_WARNING`.

    Returns:
        A :class:`ManifestWriteResult`.

    Raises:
        SealedCapsuleError: The capsule is sealed and ``force_unseal`` is False.
        UnsafeManifestPathError: The manifest or capsule directory is a symlink
            or otherwise not a regular path.
        ManifestWriteError: The write itself failed; the original is intact.
    """
    target = capsule_dir / MANIFEST_NAME
    mode = _check_paths(capsule_dir, target)
    sealed = is_sealed(capsule_dir)
    if sealed and not force_unseal:
        raise SealedCapsuleError(
            "capsule is NovaSeal-sealed (.seal/ present); rewriting capsule.yaml would "
            "invalidate the seal and make `nova verify` report tampering. Refusing. "
            f"Pass {FORCE_UNSEAL_FLAG} to write anyway and re-issue the seal afterwards."
        )
    fd, tmp_name = tempfile.mkstemp(prefix=".capsule.yaml.", dir=capsule_dir)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        # Re-check right before the swap to narrow the lstat→replace window.
        _check_paths(capsule_dir, target)
        atomic_replace(tmp, target)
    except ManifestWriteError:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ManifestWriteError(f"cannot write {MANIFEST_NAME}: {type(exc).__name__}") from exc
    return ManifestWriteResult(path=target, was_sealed=sealed)
