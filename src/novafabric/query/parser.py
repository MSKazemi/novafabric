"""Closed allow-list parser for the Capsule Query DSL v0 (ADR-0129 P1).

Accepts the two equivalent query forms — CLI flag strings and the JSON/YAML
query object — and compiles both into the same :class:`~novafabric.query.model.QueryPlan`.
Anything outside the allow-list of ``design/spec/capsule-query-dsl-v0.md`` is a
hard :class:`~novafabric.query.errors.QueryParseError` **before any storage
access** (fail closed; the offending token is named).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from novafabric.query.errors import QueryParseError
from novafabric.query.model import (
    AGGREGATE_FUNCS,
    DEFAULT_LIMIT,
    DIMENSIONS,
    LOG_LEVEL_ALIASES,
    LOG_LEVEL_RANKS,
    MAX_LIMIT,
    NUMERIC_METRICS,
    OPERATORS,
    QUERY_SCHEMA_VERSION,
    Aggregate,
    OrderBy,
    Predicate,
    QueryPlan,
)

#: The only keys a query object may carry — no fifth clause, no raw SQL surface.
_ALLOWED_QUERY_KEYS = frozenset(
    {"schema_version", "select", "where", "group_by", "since", "until", "limit", "order_by"}
)

_SELECT_RE = re.compile(
    r"^\s*(?:"
    r"(?P<count>count)\s*\(\s*\)"
    r"|(?P<func>[A-Za-z_][A-Za-z0-9_]*|p\d{1,2})\s*\(\s*(?P<metric>[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\[(?P<score_name>[A-Za-z0-9_.\-]+)\])?)\s*\)"
    r")\s*(?:AS\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*)?$"
)
_PERCENTILE_RE = re.compile(r"^p\d{1,2}$")
_IN_PRED_RE = re.compile(r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s+IN\s*\((?P<vals>.*)\)\s*$")
_CMP_PRED_RE = re.compile(
    r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op>!=|<=|>=|=|<|>)\s*(?P<value>.+?)\s*$"
)
_DURATION_RE = re.compile(r"^(?P<n>\d+)(?P<unit>[dhms])$")
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


def parse_select_item(text: str) -> Aggregate:
    """Parse one ``select`` item, e.g. ``avg(cost) AS avg_cost`` or ``count()``."""
    match = _SELECT_RE.match(text)
    if match is None:
        raise QueryParseError(f"invalid select expression: {text!r}")
    if match.group("count"):
        alias = match.group("alias") or "count()"
        return Aggregate(func="count", metric=None, score_name=None, alias=alias)

    func = match.group("func")
    if func not in AGGREGATE_FUNCS and not _PERCENTILE_RE.match(func):
        raise QueryParseError(
            f"unknown aggregate function {func!r} in {text.strip()!r}; "
            f"allowed: count, {', '.join(AGGREGATE_FUNCS)}, pXX"
        )

    metric = match.group("metric")
    score_name = match.group("score_name")
    if score_name is not None:
        base = metric.split("[", 1)[0]
        if base != "score":
            raise QueryParseError(f"unknown metric {metric!r} in {text.strip()!r}")
        metric = "score"
    elif metric == "score":
        raise QueryParseError(
            f"bare 'score' in {text.strip()!r}: eval scores are per named metric — "
            "use score[<name>], e.g. avg(score[faithfulness])"
        )
    elif metric not in NUMERIC_METRICS:
        raise QueryParseError(
            f"unknown metric {metric!r} in {text.strip()!r}; "
            f"allowed: {', '.join(NUMERIC_METRICS)}, score[<name>]"
        )

    default_alias = f"{func}(score[{score_name}])" if metric == "score" else f"{func}({metric})"
    alias = match.group("alias") or default_alias
    return Aggregate(func=func, metric=metric, score_name=score_name, alias=alias)


def parse_select(value: str | list[str]) -> tuple[Aggregate, ...]:
    """Parse the ``select`` clause — a comma-separated flag string or a list."""
    items = list(value.split(",")) if isinstance(value, str) else list(value)
    items = [s for s in (i.strip() for i in items) if s]
    if not items:
        raise QueryParseError("select requires at least one aggregate expression")
    aggregates = tuple(parse_select_item(item) for item in items)
    aliases = [agg.alias for agg in aggregates]
    duplicates = {a for a in aliases if aliases.count(a) > 1}
    if duplicates:
        raise QueryParseError(f"duplicate select alias: {', '.join(sorted(duplicates))}")
    return aggregates


def _normalize_log_level(value: str) -> str:
    level = LOG_LEVEL_ALIASES.get(value.lower(), value.lower())
    if level not in LOG_LEVEL_RANKS:
        raise QueryParseError(
            f"unknown log_level value {value!r}; allowed: {', '.join(LOG_LEVEL_RANKS)}"
        )
    return level


def parse_predicate(text: str) -> Predicate:
    """Parse one ``where`` predicate — ``FIELD OP VALUE`` or ``FIELD IN (...)``."""
    in_match = _IN_PRED_RE.match(text)
    if in_match is not None:
        dimension = in_match.group("field")
        if dimension not in DIMENSIONS:
            raise QueryParseError(
                f"unknown filter field {dimension!r}; allowed: {', '.join(DIMENSIONS)}"
            )
        values = tuple(_unquote(v) for v in in_match.group("vals").split(",") if v.strip())
        if not values:
            raise QueryParseError(f"IN (...) requires at least one value: {text.strip()!r}")
        if dimension == "log_level":
            values = tuple(_normalize_log_level(v) for v in values)
        return Predicate(dimension=dimension, op="IN", values=values)

    cmp_match = _CMP_PRED_RE.match(text)
    if cmp_match is None:
        raise QueryParseError(
            f"invalid filter predicate: {text.strip()!r} (expected FIELD OP VALUE "
            f"with OP in {{{', '.join(OPERATORS)}, IN}})"
        )
    dimension = cmp_match.group("field")
    if dimension not in DIMENSIONS:
        raise QueryParseError(
            f"unknown filter field {dimension!r}; allowed: {', '.join(DIMENSIONS)}"
        )
    op = cmp_match.group("op")
    value = _unquote(cmp_match.group("value"))
    if not value:
        raise QueryParseError(f"empty filter value in {text.strip()!r}")
    if dimension == "log_level":
        value = _normalize_log_level(value)
    return Predicate(dimension=dimension, op=op, value=value)


def parse_where(value: str | list[str] | None) -> tuple[Predicate, ...]:
    """Parse the ``where`` clause — predicates joined by uppercase ``AND`` only."""
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [p for p in re.split(r"\s+AND\s+", value) if p.strip()]
    else:
        parts = [p for p in value if str(p).strip()]
    return tuple(parse_predicate(str(part)) for part in parts)


def parse_group_by(value: str | list[str] | None) -> tuple[str, ...]:
    """Parse the ``group_by`` clause — allow-listed dimensions only."""
    if value is None:
        return ()
    items = value.split(",") if isinstance(value, str) else list(value)
    dims: list[str] = []
    for raw in items:
        dim = str(raw).strip()
        if not dim:
            continue
        if dim in NUMERIC_METRICS or dim == "score":
            raise QueryParseError(f"numeric metric {dim!r} must not appear in group_by")
        if dim not in DIMENSIONS:
            raise QueryParseError(
                f"unknown group_by dimension {dim!r}; allowed: {', '.join(DIMENSIONS)}"
            )
        if dim not in dims:
            dims.append(dim)
    return tuple(dims)


def parse_duration(value: str) -> timedelta | None:
    """Parse ``7d`` / ``24h`` / ``P30D``-style durations; None if not a duration."""
    short = _DURATION_RE.match(value)
    if short is not None:
        n = int(short.group("n"))
        unit = short.group("unit")
        return timedelta(**{{"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}[unit]: n})
    if value.startswith("P"):
        iso = _ISO_DURATION_RE.match(value)
        if iso is None or not any(iso.groupdict().values()):
            raise QueryParseError(f"invalid ISO-8601 duration: {value!r}")
        parts = {k: int(v) for k, v in iso.groupdict().items() if v is not None}
        return timedelta(**parts)
    return None


def parse_timestamp(value: str, *, clause: str) -> datetime:
    """Parse an RFC 3339 timestamp; naive values are treated as UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueryParseError(f"invalid {clause} timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def validate_since(value: str | None) -> str | None:
    """Validate ``since`` — duration shorthand, ISO-8601 duration, or timestamp."""
    if value is None:
        return None
    value = value.strip()
    if parse_duration(value) is not None:
        return value
    parse_timestamp(value, clause="since")
    return value


def validate_until(value: str | None) -> str | None:
    """Validate ``until`` — RFC 3339 timestamp only."""
    if value is None:
        return None
    value = value.strip()
    parse_timestamp(value, clause="until")
    return value


def _parse_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if not isinstance(value, int) or isinstance(value, bool):
        raise QueryParseError(f"limit must be an integer, got {value!r}")
    if value < 1 or value > MAX_LIMIT:
        raise QueryParseError(f"limit must be between 1 and {MAX_LIMIT}, got {value}")
    return value


def _parse_order_by(
    value: str | dict[str, Any] | None,
    selects: tuple[Aggregate, ...],
    group_by: tuple[str, ...],
) -> OrderBy:
    if value is None:
        return OrderBy(by=selects[0].alias, direction="desc")
    if isinstance(value, dict):
        unknown = set(value) - {"by", "direction"}
        if unknown:
            raise QueryParseError(f"unknown order_by key: {', '.join(sorted(unknown))}")
        by = str(value.get("by", "")).strip()
        direction = str(value.get("direction", "desc")).strip().lower()
    else:
        parts = str(value).split()
        if len(parts) == 1:
            by, direction = parts[0], "desc"
        elif len(parts) == 2:
            by, direction = parts[0], parts[1].lower()
        else:
            raise QueryParseError(f"invalid order_by: {value!r}")
    if direction not in ("asc", "desc"):
        raise QueryParseError(f"order_by direction must be 'asc' or 'desc', got {direction!r}")
    allowed = {agg.alias for agg in selects} | set(group_by)
    if by not in allowed:
        raise QueryParseError(
            f"order_by {by!r} is not a selected aggregate alias or group_by dimension"
        )
    return OrderBy(by=by, direction=direction)


def build_plan(
    *,
    select: str | list[str],
    where: str | list[str] | None = None,
    group_by: str | list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    order_by: str | dict[str, Any] | None = None,
) -> QueryPlan:
    """Compile clause values (flag strings or query-object fields) into a plan."""
    selects = parse_select(select)
    predicates = parse_where(where)
    dims = parse_group_by(group_by)
    return QueryPlan(
        selects=selects,
        where=predicates,
        group_by=dims,
        since=validate_since(since),
        until=validate_until(until),
        limit=_parse_limit(limit),
        order_by=_parse_order_by(order_by, selects, dims),
    )


def check_query_object_keys(obj: dict[str, Any], *, source: str = "query object") -> None:
    """Reject unknown clauses and version-mismatched query objects (fail closed).

    Unknown clauses are rejected (no fifth clause, no raw SQL surface);
    ``schema_version``, when present, must match the current version.
    """
    unknown = set(obj) - _ALLOWED_QUERY_KEYS
    if unknown:
        raise QueryParseError(
            f"unknown query clause in {source}: "
            f"{', '.join(sorted(str(k) for k in unknown))} "
            f"(allowed: {', '.join(sorted(_ALLOWED_QUERY_KEYS))})"
        )
    version = obj.get("schema_version")
    if version is not None and version != QUERY_SCHEMA_VERSION:
        raise QueryParseError(
            f"unsupported query schema_version {version!r} in {source}; "
            f"expected {QUERY_SCHEMA_VERSION!r}"
        )


def validate_query_object(obj: dict[str, Any], *, source: str = "query object") -> QueryPlan:
    """Key-validate and fully compile a query object (fail closed, no storage access).

    The one-call validation surface for consumers that persist query objects
    (ADR-0130 saved views): anything outside the closed allow-list — an unknown
    clause, an unknown metric/dimension/operator, a bad time window — raises
    :class:`~novafabric.query.errors.QueryParseError` before anything is stored
    or executed.
    """
    check_query_object_keys(obj, source=source)
    return plan_from_query_object(obj)


def load_query_file(path: str | Path) -> dict[str, Any]:
    """Load and key-validate a JSON/YAML query object file.

    Unknown clauses are rejected (no fifth clause, no raw SQL surface);
    ``schema_version``, when present, must match the current version.
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise QueryParseError(f"cannot read query file {file_path}: {exc}") from exc
    try:
        if file_path.suffix.lower() == ".json":
            obj = json.loads(text)
        else:
            obj = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise QueryParseError(f"query file {file_path} is not valid JSON/YAML: {exc}") from exc
    if not isinstance(obj, dict):
        raise QueryParseError(f"query file {file_path} must contain a single query object")
    check_query_object_keys(obj, source=f"query file {file_path}")
    return obj


def plan_from_query_object(
    obj: dict[str, Any],
    *,
    select: str | None = None,
    where: str | None = None,
    group_by: str | list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    order_by: str | None = None,
) -> QueryPlan:
    """Build a plan from a query object, with CLI flags overriding file fields."""
    merged_select = select if select is not None else obj.get("select")
    if merged_select is None:
        raise QueryParseError("query has no select clause (required)")
    if not isinstance(merged_select, (str, list)):
        raise QueryParseError("select must be a string or a list of strings")
    merged_where = where if where is not None else obj.get("where")
    if merged_where is not None and not isinstance(merged_where, (str, list)):
        raise QueryParseError("where must be a string or a list of strings")
    merged_group_by = group_by if group_by else obj.get("group_by")
    if merged_group_by is not None and not isinstance(merged_group_by, (str, list)):
        raise QueryParseError("group_by must be a list of dimension names")
    merged_limit = limit if limit is not None else obj.get("limit")
    if merged_limit is not None and not isinstance(merged_limit, int):
        raise QueryParseError(f"limit must be an integer, got {merged_limit!r}")
    merged_order_by = order_by if order_by is not None else obj.get("order_by")
    if merged_order_by is not None and not isinstance(merged_order_by, (str, dict)):
        raise QueryParseError("order_by must be a string or {by, direction} object")
    merged_since = since if since is not None else obj.get("since")
    if merged_since is not None and not isinstance(merged_since, str):
        raise QueryParseError(f"since must be a string, got {merged_since!r}")
    merged_until = until if until is not None else obj.get("until")
    if merged_until is not None and not isinstance(merged_until, str):
        raise QueryParseError(f"until must be a string, got {merged_until!r}")
    return build_plan(
        select=merged_select,
        where=merged_where,
        group_by=merged_group_by,
        since=merged_since,
        until=merged_until,
        limit=merged_limit,
        order_by=merged_order_by,
    )
