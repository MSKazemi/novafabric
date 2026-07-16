"""``SavedView`` envelope model — a named, persisted ADR-0129 query (ADR-0130).

A saved view is **data, not code**: a small file wrapping a verbatim ADR-0129
query object plus a name and optional, purely advisory display preferences
(ADR-0130 I1–I3). This module models the *envelope* only — the nested
``query`` object is carried opaquely (any JSON object) so the envelope stays
valid regardless of how the query grammar evolves. The query itself is
fail-closed validated by the ADR-0129 parser at **save time** and again at
**run time** (``store.save_view`` / ``nova view run``), never here.

Wire contract: ``schemas/saved-view.schema.json`` (graduated from
``design/spec/schemas/`` on ADR acceptance); spec:
``design/spec/saved-views-v0.md``. Status: **experimental**.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

VIEW_SCHEMA_VERSION: Literal["0.1.0"] = "0.1.0"

#: ``view_id`` slug pattern — mirrors ``saved-view.schema.json``.
VIEW_ID_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
_VIEW_ID_RE = re.compile(VIEW_ID_PATTERN)

#: Envelope fields that define the view's *content* for hashing purposes.
#: Provenance timestamps (``created_at``/``updated_at``) and author identity
#: (``created_by``) are deliberately excluded: the hash identifies the
#: definition, so the same view saved on two machines hashes identically.
_HASH_FIELDS: tuple[str, ...] = (
    "schema_version",
    "view_id",
    "name",
    "query",
    "description",
    "display",
    "tags",
)


class SortKey(BaseModel):
    """One advisory display sort key: ``{field, order}``."""

    model_config = ConfigDict(extra="forbid")

    field: str
    order: Literal["asc", "desc"]


class DisplayPrefs(BaseModel):
    """Advisory presentation preferences (ADR-0130 I3).

    Affects only rendering — which columns, row order, output format. It never
    changes which capsules match, and any consumer MAY ignore it.
    """

    model_config = ConfigDict(extra="forbid")

    columns: list[str] | None = None
    sort: list[SortKey] | None = None
    format: Literal["table", "json", "csv"] | None = None


class SavedView(BaseModel):
    """One saved view — a single YAML document / JSON object on disk.

    Top-level closed (unknown keys rejected), matching the Run Capsule
    convention. The ``query`` block is a verbatim ADR-0129 query object
    (invariant I1) and is validated by the ADR-0129 parser at save/run time.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = VIEW_SCHEMA_VERSION
    view_id: str = Field(pattern=VIEW_ID_PATTERN)
    name: str = Field(min_length=1)
    query: dict[str, Any]
    created_at: str
    description: str | None = None
    created_by: str | None = None
    updated_at: str | None = None
    display: DisplayPrefs | None = None
    tags: list[str] | None = None

    def to_document(self) -> dict[str, Any]:
        """The on-disk document form (JSON-safe, unset optionals omitted)."""
        return self.model_dump(mode="json", exclude_none=True)


def is_valid_view_id(candidate: str) -> bool:
    """True when ``candidate`` matches the ``view_id`` slug pattern."""
    return _VIEW_ID_RE.match(candidate) is not None


def view_hash(view: SavedView) -> str:
    """Content hash of the view *definition* — ``sha256:<hex>``.

    Computed over the canonical JSON (sorted keys, compact separators) of the
    definition fields only (:data:`_HASH_FIELDS`). Timestamps and author are
    excluded, so the hash is stable across machines and re-saves of the same
    definition — a report can record exactly which view version produced it.
    """
    document = view.model_dump(mode="json", exclude_none=True)
    payload = {key: document.get(key) for key in _HASH_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
