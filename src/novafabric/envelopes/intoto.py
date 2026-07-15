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

from novafabric.evidence.intoto import INTOTO_STATEMENT_TYPE

CAPSULE_PREDICATE_TYPE = "novafabric.dev/capsule/v1"
CAPSULE_MAPPING_VERSION = "capsule-predicate/1"
PREDICATE_SCHEMA_VERSION = "1.0.0"


class SubjectDigestMismatch(Exception):
    """Raised when a subject's recomputed digest disagrees with an expected one."""


def _capsule_files(capsule_dir: Path) -> list[Path]:
    """All capsule files, sorted by relative path (matches ``capsule_merkle_root``)."""
    return [p for p in sorted(capsule_dir.rglob("*")) if p.is_file()]


def _read_run_id(capsule_dir: Path) -> str:
    for name in ("capsule.yaml", "capsule.json"):
        path = capsule_dir / name
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and data.get("run_id"):
                return str(data["run_id"])
    return ""


def capsule_subjects(capsule_dir: str | Path) -> list[dict[str, Any]]:
    """in-toto ``subject[]`` for a capsule: one ``{name, digest.sha256}`` per file."""
    capsule_dir = Path(capsule_dir)
    subjects: list[dict[str, Any]] = []
    for path in _capsule_files(capsule_dir):
        rel = path.relative_to(capsule_dir).as_posix()
        subjects.append(
            {"name": rel, "digest": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
        )
    return subjects


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
