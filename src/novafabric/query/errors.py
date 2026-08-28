"""Named exception classes for the offline metrics query DSL (ADR-0129)."""

from __future__ import annotations


class QueryError(Exception):
    """Base class for all `nova query` errors."""


class QueryParseError(QueryError):
    """A query failed allow-list validation before any storage access.

    Raised for any field, operator, function, metric, or clause outside the
    closed allow-list of ``the private design/spec/capsule-query-dsl-v0.md`` — the query
    fails closed with the offending token named.
    """


class QueryIndexError(QueryError):
    """Building the derived index from the capsule directory failed.

    Index-build failure degrades to a clear error, never a silent wrong
    answer (ADR-0129 D3).
    """


class QueryExecutionError(QueryError):
    """Executing a parsed query failed (e.g. group-cardinality cap exceeded)."""
