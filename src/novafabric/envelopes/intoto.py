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

"""in-toto capsule Statement — ``novafabric.dev/capsule/v1`` (NF-030, ADR-0096).

Emits a portable in-toto Statement v1 whose ``subject[]`` are the capsule's artifacts,
each digest being the authoritative sha256 of the file's bytes (the ADR-0087 completeness
digest). An off-the-shelf in-toto verifier can then verify the attestation, and the
Statement itself is the DSSE payload (sign with :func:`novafabric.evidence.intoto.dsse_sign`,
``payloadType application/vnd.in-toto+json``) — one signed portable attestation, no second
DSSE path.

Requirement 5 (digest fidelity): if a caller supplies ``expected_digests`` (e.g. the
digests recorded by the completeness assertion / bundle manifest), any mismatch raises
:class:`SubjectDigestMismatch` and no Statement is produced — we never emit a
verifying-but-wrong attestation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from novafabric._capsule_fs import list_capsule, open_capsule_file, read_required_capsule_member
from novafabric.evidence.intoto import INTOTO_STATEMENT_TYPE

CAPSULE_PREDICATE_TYPE = "novafabric.dev/capsule/v1"
CAPSULE_MAPPING_VERSION = "capsule-predicate/1"
PREDICATE_SCHEMA_VERSION = "1.0.0"


class SubjectDigestMismatch(Exception):
    """Raised when a subject's recomputed digest disagrees with an expected one."""


#: The ``operation`` named in a ``_capsule_fs`` refusal.
_OPERATION = "an in-toto statement"


def _capsule_files(capsule_dir: Path) -> list[str]:
    """Every regular file's capsule-relative path, in ``capsule_merkle_root`` order.

    Walks without following a link and raises
    :class:`~novafabric._capsule_fs.CapsuleLinkError` naming every symlink, as
    the evidence export does: a link cannot be a subject, and hashing its
    target would attest a file outside the capsule. FIFOs, sockets and devices
    are not capsule content and are left out, as every exporter leaves them out.
    """
    listing = list_capsule(capsule_dir, operation=_OPERATION)
    return sorted(listing.files, key=lambda rel: rel.split("/"))


def _sha256_file(capsule_dir: Path, rel: str) -> str:
    """Streamed sha256 of *rel*, opened without following a link or blocking."""
    digest = hashlib.sha256()
    with open_capsule_file(capsule_dir, rel, operation=_OPERATION) as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_run_id(capsule_dir: Path) -> str:
    # Never through a link or into a FIFO; a manifest that is present but
    # unusable raises CapsuleRequiredFileError instead of an empty run id.
    for name in ("capsule.yaml", "capsule.json"):
        text = read_required_capsule_member(capsule_dir / name, operation="an in-toto statement")
        if text is not None:
            data = yaml.safe_load(text) or {}
            if isinstance(data, dict) and data.get("run_id"):
                return str(data["run_id"])
    return ""


def capsule_subjects(capsule_dir: str | Path) -> list[dict[str, Any]]:
    """in-toto ``subject[]`` for a capsule: one ``{name, digest.sha256}`` per file.

    Raises :class:`~novafabric._capsule_fs.CapsuleLinkError` when the capsule
    holds a symlink (never followed), and
    :class:`~novafabric._capsule_fs.CapsuleSpecialFileError` when a listed file
    was swapped for a FIFO or device before it was read (never blocked on).
    """
    capsule_dir = Path(capsule_dir)
    return [
        {"name": rel, "digest": {"sha256": _sha256_file(capsule_dir, rel)}}
        for rel in _capsule_files(capsule_dir)
    ]


def capsule_statement(
    capsule_dir: str | Path,
    *,
    run_id: str | None = None,
    novafabric_version: str = "0.0.0",
    created_at: str | None = None,
    expected_digests: dict[str, str] | None = None,
    extra_predicate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``novafabric.dev/capsule/v1`` in-toto Statement for a capsule.

    ``expected_digests`` maps a subject name → expected ``sha256`` (with or without the
    ``sha256:`` prefix); any mismatch raises :class:`SubjectDigestMismatch` (req 5).
    """
    capsule_dir = Path(capsule_dir)
    subjects = capsule_subjects(capsule_dir)
    if expected_digests:
        for subject in subjects:
            expected = expected_digests.get(subject["name"])
            if expected is None:
                continue
            expected = expected.removeprefix("sha256:")
            actual = subject["digest"]["sha256"]
            if expected != actual:
                raise SubjectDigestMismatch(
                    f"subject {subject['name']!r}: expected {expected}, recomputed {actual}"
                )
    rid = run_id or _read_run_id(capsule_dir)
    predicate: dict[str, Any] = {
        "schemaVersion": PREDICATE_SCHEMA_VERSION,
        "capsuleId": rid,
        "runId": rid,
        "novafabricVersion": novafabric_version,
        "mappingVersion": CAPSULE_MAPPING_VERSION,
    }
    if created_at is not None:
        predicate["createdAt"] = created_at
    if extra_predicate:
        predicate.update(extra_predicate)
    return {
        "_type": INTOTO_STATEMENT_TYPE,
        "subject": subjects,
        "predicateType": CAPSULE_PREDICATE_TYPE,
        "predicate": predicate,
    }
