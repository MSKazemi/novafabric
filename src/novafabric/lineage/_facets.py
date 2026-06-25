# src/novafabric/lineage/_facets.py
"""Column-level lineage facets (ADR-0090 Half 1, gap-003).

A conservative, stdlib-only extractor pulls table/column *names* (never
values — ADR-0021 §4) from SQL statements captured in ``tool-calls.jsonl``,
and attaches them to lineage edges under the existing additive ``facets``
dict, key ``"columns"``.

Invariants (ADR-0090): additive only (I-1); fail-open — any input yields a
facet list or ``None``, never an exception (I-2); names only (I-3);
round-trips through the store intact (I-4); semantics mirror the OpenLineage
``ColumnLineageDatasetFacet`` where the models overlap (I-5).

The extractor deliberately misses CTEs, subqueries, and ``SELECT *``
resolution; ``sqlglot`` (MIT, Tier A) is the documented upgrade path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

#: namespaced key inside ``LineageEdge.facets``
COLUMN_FACET_KEY = "columns"

#: a pathological statement cannot bloat an edge (ADR-0090 consequences)
MAX_COLUMNS_PER_FACET = 64

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_QUALIFIED = rf"{_IDENT}(?:\.{_IDENT})*"

_SELECT_RE = re.compile(
    rf"^\s*select\s+(?P<cols>.+?)\s+from\s+(?P<table>{_QUALIFIED})",
    re.IGNORECASE | re.DOTALL,
)
_INSERT_RE = re.compile(
    rf"^\s*insert\s+into\s+(?P<table>{_QUALIFIED})\s*\((?P<cols>[^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)
_UPDATE_RE = re.compile(
    rf"^\s*update\s+(?P<table>{_QUALIFIED})\s+set\s+(?P<assigns>.+?)(?:\swhere\s|$)",
    re.IGNORECASE | re.DOTALL,
)
_DELETE_RE = re.compile(
    rf"^\s*delete\s+from\s+(?P<table>{_QUALIFIED})", re.IGNORECASE
)
_ASSIGN_RE = re.compile(rf"({_IDENT})\s*=", re.IGNORECASE)
_SQL_START_RE = re.compile(
    r"^\s*(select|insert|update|delete)\b", re.IGNORECASE
)

#: bound the extractor's work — anything longer is not a captured statement
_MAX_SQL_LENGTH = 100_000


class ColumnFacet(BaseModel):
    """Column **names** touched by one captured SQL statement (ADR-0090)."""

    table: str
    columns: list[str] = Field(default_factory=list)
    access: Literal["read", "write"]
    confidence: Literal["exact", "heuristic"]

    @field_validator("columns")
    @classmethod
    def _names_only(cls, v: list[str]) -> list[str]:
        return [c for c in v if re.fullmatch(_QUALIFIED, c)]


def _split_idents(raw: str) -> tuple[list[str], bool]:
    """Split a comma list into plain identifiers.

    Returns (identifiers, exact) — *exact* is False when any item was not a
    plain identifier (expression, function, literal) and was dropped.
    """
    idents: list[str] = []
    exact = True
    for item in raw.split(","):
        item = item.strip()
        if re.fullmatch(_QUALIFIED, item):
            idents.append(item)
        else:
            exact = False
    return idents, exact


def extract_column_facet(sql: str) -> ColumnFacet | None:
    """Best-effort column extraction from one SQL statement.

    Conservative by design: returns ``None`` for anything it cannot parse
    confidently (including ``SELECT *``).  Never raises (I-2).
    """
    try:
        if not isinstance(sql, str) or len(sql) > _MAX_SQL_LENGTH:
            return None
        if not _SQL_START_RE.match(sql):
            return None

        m = _SELECT_RE.match(sql)
        if m:
            cols, exact = _split_idents(m.group("cols"))
            if not cols:
                return None  # SELECT * or pure expressions — unresolvable
            return _bounded(m.group("table"), cols, "read", exact)

        m = _INSERT_RE.match(sql)
        if m:
            cols, exact = _split_idents(m.group("cols"))
            if not cols:
                return None
            return _bounded(m.group("table"), cols, "write", exact)

        m = _UPDATE_RE.match(sql)
        if m:
            cols = _ASSIGN_RE.findall(m.group("assigns"))
            if not cols:
                return None
            return _bounded(m.group("table"), cols, "write", True)

        m = _DELETE_RE.match(sql)
        if m:
            # row-level write; column set unknown
            return ColumnFacet(
                table=m.group("table"),
                columns=[],
                access="write",
                confidence="heuristic",
            )
        return None
    except Exception:  # noqa: BLE001 — I-2: fail-open, never raise
        return None


def _bounded(
    table: str, columns: list[str], access: Literal["read", "write"], exact: bool
) -> ColumnFacet:
    confidence: Literal["exact", "heuristic"] = "exact" if exact else "heuristic"
    if len(columns) > MAX_COLUMNS_PER_FACET:
        columns = columns[:MAX_COLUMNS_PER_FACET]
        confidence = "heuristic"
    return ColumnFacet(
        table=table, columns=columns, access=access, confidence=confidence
    )


def _iter_strings(value: Any, _depth: int = 0) -> list[str]:
    if _depth > 6:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            s for v in value.values() for s in _iter_strings(v, _depth + 1)
        ]
    if isinstance(value, list):
        return [s for v in value[:100] for s in _iter_strings(v, _depth + 1)]
    return []


def facets_from_tool_calls(capsule_dir: Path) -> list[ColumnFacet]:
    """Extract column facets from a capsule's ``tool-calls.jsonl``.

    Runs at lineage-inference time, never in the capture hot path.
    Fail-open: unreadable file, bad JSON, or unparseable SQL all yield the
    empty list.
    """
    path = capsule_dir / "tool-calls.jsonl"
    facets: list[ColumnFacet] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        for text in _iter_strings(record.get("arguments")):
            facet = extract_column_facet(text)
            if facet is None:
                continue
            key = (facet.table, facet.access, tuple(facet.columns))
            if key in seen:
                continue
            seen.add(key)
            facets.append(facet)
    return facets


def attach_column_facets(
    edges: list[Any], facets: list[ColumnFacet]
) -> None:
    """Attach *facets* to every edge of the capsule being inferred.

    Slice-1 semantics: the facet answers "the run behind this edge touched
    these tables/columns" — uniform attachment per run, refined when the
    sqlglot upgrade path lands.  Edges with existing facets keep them
    (namespaced key, additive only).
    """
    if not facets:
        return
    payload = [f.model_dump() for f in facets]
    for edge in edges:
        existing = dict(edge.facets) if edge.facets else {}
        existing[COLUMN_FACET_KEY] = payload
        edge.facets = existing
