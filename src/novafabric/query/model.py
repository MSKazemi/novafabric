"""Query-plan model for the offline metrics query DSL (ADR-0129).

The plan is the parsed, validated form of a Capsule Query — exactly four
clauses (``select`` / ``where`` / ``group_by`` / time window) drawn from a
closed allow-list (``the private design/spec/capsule-query-dsl-v0.md``). Anything outside
the allow-list never reaches a plan: the parser rejects it first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

QUERY_SCHEMA_VERSION = "0.1.0"

#: Allow-listed filter / group-by dimensions (spec §Filters).
DIMENSIONS: tuple[str, ...] = (
    "asset",
    "deployment_environment",
    "variant",
    "log_level",
    "model",
    "model_id",
    "status",
    "tag",
)

#: Allow-listed numeric metrics (spec §Aggregates). ``score`` additionally
#: requires a ``score[<name>]`` qualifier — a bare ``score`` is rejected.
NUMERIC_METRICS: tuple[str, ...] = (
    "cost",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "latency",
)

#: Allow-listed non-percentile aggregate functions.
AGGREGATE_FUNCS: tuple[str, ...] = ("sum", "avg", "min", "max")

#: Allow-listed comparison operators (``IN`` is parsed separately).
OPERATORS: tuple[str, ...] = ("=", "!=", "<", "<=", ">", ">=")

DEFAULT_LIMIT = 100
MAX_LIMIT = 10_000
#: Hard cap on group cardinality — a pathological ``group_by`` is refused,
#: never an unbounded scan (ADR-0129 D3).
MAX_GROUPS = 10_000

#: ADR-0127 observation log-level severity order (``warning`` accepted as an
#: alias of ``warn`` when parsing filter values).
LOG_LEVEL_RANKS: dict[str, int] = {"debug": 0, "info": 1, "warn": 2, "error": 3}
LOG_LEVEL_ALIASES: dict[str, str] = {"warning": "warn"}


@dataclass(frozen=True)
class Aggregate:
    """One parsed ``select`` item, e.g. ``avg(cost) AS avg_cost``."""

    func: str  # "count" | "sum" | "avg" | "min" | "max" | "pNN"
    metric: str | None  # None for count(); "score" carries score_name
    score_name: str | None
    alias: str

    @property
    def expression(self) -> str:
        """Canonical expression text without the alias, e.g. ``avg(cost)``."""
        if self.func == "count":
            return "count()"
        metric = f"score[{self.score_name}]" if self.metric == "score" else self.metric
        return f"{self.func}({metric})"

    def normalized(self) -> str:
        """Spec-normalized ``select`` item (``EXPR AS alias`` when aliased)."""
        if self.alias == self.expression:
            return self.expression
        return f"{self.expression} AS {self.alias}"


@dataclass(frozen=True)
class Predicate:
    """One parsed ``where`` predicate — ``FIELD OP VALUE`` or ``FIELD IN (...)``."""

    dimension: str
    op: str  # one of OPERATORS or "IN"
    value: str | None = None
    values: tuple[str, ...] = ()

    def normalized(self) -> str:
        """Spec-normalized predicate text, e.g. ``asset = summarizer``."""
        if self.op == "IN":
            return f"{self.dimension} IN ({', '.join(self.values)})"
        return f"{self.dimension} {self.op} {self.value}"


@dataclass(frozen=True)
class OrderBy:
    """Result ordering — by a selected aggregate alias or group-by dimension."""

    by: str
    direction: str = "desc"  # "asc" | "desc"


@dataclass(frozen=True)
class QueryPlan:
    """A fully validated query, ready for execution.

    ``since`` / ``until`` are kept in their raw spec form; the executor
    resolves them against *now* at run time (so a plan stays deterministic
    and testable).
    """

    selects: tuple[Aggregate, ...]
    where: tuple[Predicate, ...] = ()
    group_by: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None
    limit: int = DEFAULT_LIMIT
    order_by: OrderBy = field(default_factory=lambda: OrderBy(by="", direction="desc"))

    def to_query_object(self) -> dict[str, Any]:
        """The normalized query object echoed back in the result JSON."""
        obj: dict[str, Any] = {
            "schema_version": QUERY_SCHEMA_VERSION,
            "select": [agg.normalized() for agg in self.selects],
        }
        if self.where:
            obj["where"] = [pred.normalized() for pred in self.where]
        if self.group_by:
            obj["group_by"] = list(self.group_by)
        if self.since is not None:
            obj["since"] = self.since
        if self.until is not None:
            obj["until"] = self.until
        obj["limit"] = self.limit
        if self.order_by.by:
            obj["order_by"] = {"by": self.order_by.by, "direction": self.order_by.direction}
        return obj
