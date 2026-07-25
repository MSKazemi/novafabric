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
"""GPAI Art. 53 Model Documentation Form exporter (ADR-0107 §NF-093).

Article 53(1) requires providers of general-purpose AI models to draw up and keep up
to date the model's technical documentation. NovaFabric keeps it as a **sealed,
hash-chained revision history**:

* each revision is canonically hashed (via the shared
  :mod:`novafabric._hashutil`) and carries its predecessor's digest in ``prev_digest``
  — a tamper-evident **material-change history** (:func:`verify_history` recomputes the
  chain and rejects any silent edit or broken link);
* each revision carries a **10-year** ``retention_until`` (Art. 53 documentation
  retention); and
* any two revisions are field-level **diffable** (:func:`diff_revisions`).

Pure-code and offline: no infrastructure, no new dependencies. This completes ADR-0107's
pure-code exporter set (NF-090/091/093/094/095; NF-092 served by ``AnnexIVExporter``);
NF-097 remains future design.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from novafabric._hashutil import sha256_prefixed

#: Art. 53 documentation retention period, in years.
GPAI_ART53_RETENTION_YEARS = 10


class FieldChangeKind(str, Enum):
    """How a field changed between two revisions."""

    added = "added"
    removed = "removed"
    modified = "modified"


class Gpai53FieldChange(BaseModel):
    """One field-level change between two revisions."""

    field: str
    change: FieldChangeKind


class Gpai53Revision(BaseModel):
    """One sealed revision of the Art. 53(1) documentation form."""

    revision: int
    fields: dict[str, str]
    created_at: str
    retention_until: str
    content_digest: str
    prev_digest: str | None = None


class Gpai53Form(BaseModel):
    """A GPAI Art. 53 documentation form as a sealed revision history."""

    model_name: str
    revisions: list[Gpai53Revision] = Field(default_factory=list)


def _canonical_digest(fields: Mapping[str, str]) -> str:
    """Deterministic ``sha256:`` digest of a revision's fields (sorted, compact JSON)."""
    payload = json.dumps(dict(fields), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_prefixed(payload)


def _add_years(when: datetime, years: int) -> datetime:
    """``when`` + ``years``, folding a Feb-29 anchor back to Feb-28 in a common year."""
    try:
        return when.replace(year=when.year + years)
    except ValueError:
        return when.replace(year=when.year + years, day=28)


def _retention_until(created_at: datetime) -> str:
    return _add_years(
        created_at.astimezone(timezone.utc), GPAI_ART53_RETENTION_YEARS
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seal_revision(
    *,
    revision: int,
    fields: Mapping[str, str],
    created_at: datetime,
    prev_digest: str | None,
) -> Gpai53Revision:
    return Gpai53Revision(
        revision=revision,
        fields=dict(fields),
        created_at=_iso(created_at),
        retention_until=_retention_until(created_at),
        content_digest=_canonical_digest(fields),
        prev_digest=prev_digest,
    )


def build_gpai53_form(
    model_name: str,
    *,
    initial_fields: Mapping[str, str],
    created_at: datetime,
) -> Gpai53Form:
    """Create a form with its first sealed revision.

    Raises :class:`ValueError` if ``initial_fields`` is empty — an Art. 53 form with no
    documentation content is not a form.
    """
    if not initial_fields:
        raise ValueError("initial_fields must not be empty for an Art. 53 form")
    revision = _seal_revision(
        revision=1, fields=initial_fields, created_at=created_at, prev_digest=None
    )
    return Gpai53Form(model_name=model_name, revisions=[revision])


def append_revision(
    form: Gpai53Form,
    *,
    fields: Mapping[str, str],
    created_at: datetime,
) -> Gpai53Form:
    """Seal a new revision recording a material change, chained onto the latest revision."""
    if not fields:
        raise ValueError("a revision must not have empty fields")
    latest = form.revisions[-1]
    revision = _seal_revision(
        revision=latest.revision + 1,
        fields=fields,
        created_at=created_at,
        prev_digest=latest.content_digest,
    )
    return Gpai53Form(model_name=form.model_name, revisions=[*form.revisions, revision])


def verify_history(form: Gpai53Form) -> bool:
    """Verify the sealed revision chain — content digests and predecessor links.

    Returns ``True`` only if every revision's recomputed digest matches its stored
    ``content_digest`` **and** every revision after the first carries its predecessor's
    digest in ``prev_digest``. A silent edit to a sealed revision, or a broken link,
    returns ``False``.
    """
    prev: str | None = None
    for rev in form.revisions:
        if _canonical_digest(rev.fields) != rev.content_digest:
            return False
        if rev.prev_digest != prev:
            return False
        prev = rev.content_digest
    return True


def diff_revisions(
    older: Gpai53Revision,
    newer: Gpai53Revision,
) -> list[Gpai53FieldChange]:
    """Field-level diff between two revisions; unchanged fields are not reported."""
    changes: list[Gpai53FieldChange] = []
    for key in sorted(set(older.fields) | set(newer.fields)):
        in_old = key in older.fields
        in_new = key in newer.fields
        if in_old and not in_new:
            changes.append(Gpai53FieldChange(field=key, change=FieldChangeKind.removed))
        elif in_new and not in_old:
            changes.append(Gpai53FieldChange(field=key, change=FieldChangeKind.added))
        elif older.fields[key] != newer.fields[key]:
            changes.append(Gpai53FieldChange(field=key, change=FieldChangeKind.modified))
    return changes
