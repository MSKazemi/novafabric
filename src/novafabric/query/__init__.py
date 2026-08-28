"""Offline metrics query DSL over the local capsule store (ADR-0129).

A bounded, declarative, read-only ``filter → group-by → aggregate`` surface
over *local* Run Capsules — no server, no network, no raw SQL. Spec:
``the private design/spec/capsule-query-dsl-v0.md``. Status: **experimental**.
"""

from novafabric.query.errors import (
    QueryError,
    QueryExecutionError,
    QueryIndexError,
    QueryParseError,
)
from novafabric.query.executor import run_query
from novafabric.query.model import (
    DEFAULT_LIMIT,
    MAX_GROUPS,
    MAX_LIMIT,
    QUERY_SCHEMA_VERSION,
    Aggregate,
    OrderBy,
    Predicate,
    QueryPlan,
)
from novafabric.query.parser import (
    build_plan,
    load_query_file,
    plan_from_query_object,
    validate_query_object,
)

__all__ = [
    "Aggregate",
    "DEFAULT_LIMIT",
    "MAX_GROUPS",
    "MAX_LIMIT",
    "OrderBy",
    "Predicate",
    "QUERY_SCHEMA_VERSION",
    "QueryError",
    "QueryExecutionError",
    "QueryIndexError",
    "QueryParseError",
    "QueryPlan",
    "build_plan",
    "load_query_file",
    "plan_from_query_object",
    "run_query",
    "validate_query_object",
]
