"""The capsule digest a maker-checker proposal or bypass signs (review cli-b-01).

Proposals and bypasses signed ``sha256(capsule_id)`` — a hash of the *id string* —
as their ``capsule_digest``, and nothing compared it with anything: ``nova seal
verify`` passed for a capsule that did not exist, and an approval survived any
change to the capsule. The digest is now the SHA-256 of the capsule's
``capsule.yaml``, which binds every evidence file through ``evidence_digests`` once
the capsule is finalised, and :func:`novafabric.promote.verifier.verify_sod`
recomputes it from the capsule it is given.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from novafabric._capsule_fs import CapsuleEntryError, read_capsule_file

#: A capsule manifest larger than this is refused rather than hashed.
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class CapsuleDigestError(Exception):
    """The capsule's ``capsule.yaml`` is missing or cannot be read safely."""


def capsule_content_digest(capsule_dir: Path) -> str:
    """Hex SHA-256 of *capsule_dir*'s ``capsule.yaml``, read without following links."""
    try:
        manifest = read_capsule_file(
            capsule_dir, "capsule.yaml", operation="nova seal", max_bytes=_MAX_MANIFEST_BYTES
        )
    except FileNotFoundError as exc:
        raise CapsuleDigestError(f"{capsule_dir} has no capsule.yaml") from exc
    except (OSError, ValueError, CapsuleEntryError) as exc:
        raise CapsuleDigestError(f"{capsule_dir}/capsule.yaml not read: {exc}") from exc
    return hashlib.sha256(manifest).hexdigest()


def id_stand_in_digest(capsule_id: str) -> str:
    """The digest releases up to v0.107.0 signed: a hash of the id, not of the capsule."""
    return hashlib.sha256(capsule_id.encode()).hexdigest()
