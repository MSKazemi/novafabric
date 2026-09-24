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
"""The filter-bar grammar — ADR-0232 D1/D3.

A typing convenience over the ADR-0129 DSL: ``status:error -model:gpt-4 cost:>0.5``
compiles to the same :class:`~novafabric.query.model.Predicate` tuples
``nova query --where`` produces. **Its power ceiling is deliberately the DSL's**,
and that is the load-bearing sentence:

    No predicate is expressible in the bar that is not expressible in `nova query`.

A bar that could express more would be a *second, unreviewed query surface*. The
DSL's closed allow-list is a security property, not an ergonomic one — ADR-0235
D7 leans on it when validating a widget that arrived from somewhere untrusted —
so anything the bar cannot map onto an existing predicate is a **parse error**,
never a best-effort interpretation.

## ⚠⚠ The ADR's own example violates the ADR's own rule — twice

D1 illustrates the grammar with ``status:error -model:gpt-4* cost:>0.5``. Checked
against the code on 2026-09-06, **two of those three terms are not expressible in
`nova query`**:

* ``model:gpt-4*`` — the DSL's operators are exactly ``= != < <= > >=`` plus
  ``IN``, and there is **no glob, wildcard, LIKE or fnmatch anywhere in**
  ``query/``.
* ``cost:>0.5`` — ``parse_predicate`` accepts only the eight ``DIMENSIONS``.
  **A numeric metric cannot be filtered on at all**; metrics are things the DSL
  *aggregates*, not things it filters by. ``nova query --where 'cost > 0.5'``
  raises today.

Only ``status:error`` survives.

**The rule wins over the example**, so this module is dimensions-only and both
other forms raise with a message that explains the ceiling. That is a smaller
feature than the example implies, and it is the honest one: a bar that accepted
``cost:>0.5`` would be a second query surface with semantics nothing else in the
product can reproduce — and it was caught here by the very equivalence test
written to enforce the rule, on the first run.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Final

from novafabric.query.errors import QueryParseError
from novafabric.query.model import DIMENSIONS, NUMERIC_METRICS, Predicate
from novafabric.query.parser import parse_predicate

__all__ = [
    "MAX_SUGGESTIONS",
    "ObservedValues",
    "parse_filter_bar",
    "observed_values",
]

#: ADR-0232 D3 — hard cap on a suggestion list. A naive distinct-values query
#: over an unbounded store is exactly the "read whole stores into memory and
#: reduce in Python" defect ADR-0199 named.
MAX_SUGGESTIONS: Final[int] = 100

#: ``dim:value`` / ``-dim:value`` / ``metric:>value``. The leading ``-`` is
#: negation; the optional comparison prefix is only meaningful for a numeric
#: metric and is rejected on a string dimension, because ``status:>error`` has no
#: meaning the DSL could carry.
_TERM_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<negate>-)?(?P<field>[A-Za-z_][A-Za-z0-9_]*)"
    r":(?P<op>>=|<=|!=|>|<|=)?(?P<value>.*)$"
)

#: Characters that mean "pattern" to a user and nothing to this DSL. Caught
#: explicitly so the error can say *why*, instead of the value being compared
#: literally and matching nothing.
_PATTERN_CHARS: Final[str] = "*?%"


def _fail(message: str) -> QueryParseError:
    return QueryParseError(message)


def parse_filter_bar(text: str) -> tuple[Predicate, ...]:
    """Compile a filter-bar string into DSL predicates. Fails closed.

    Terms are whitespace-separated and ANDed, matching the DSL's ``where``
    semantics. Values may be quoted to contain spaces.

    Every rejection names what is allowed, because a filter bar is a place people
    type guesses, and an error that does not teach the vocabulary just gets
    retried.
    """
    if not text or not text.strip():
        return ()

    try:
        terms = shlex.split(text)
    except ValueError as exc:
        # shlex raises on an unterminated quote. Reported rather than tolerated:
        # silently closing the quote would filter on a value the user never
        # finished typing.
        raise _fail(f"unbalanced quote in filter: {exc}") from exc

    predicates: list[Predicate] = []
    for term in terms:
        # `_parse_term` produces DSL *predicate text*, which is then handed to
        # the DSL's own parser. That is what makes D1's ceiling structural
        # rather than maintained by hand: the bar cannot accept anything
        # `nova query --where` would reject, because the same function decides.
        #
        # An earlier version duplicated the checks here and drifted immediately —
        # it accepted `log_level:value`, which the DSL refuses because log levels
        # are a closed set. Delegation removes the class, not just that instance.
        predicate_text, term_source = _parse_term(term)
        try:
            predicates.append(parse_predicate(predicate_text))
        except QueryParseError as exc:
            raise _fail(f"{term_source!r}: {exc}") from exc
    return tuple(predicates)


def _parse_term(term: str) -> tuple[str, str]:
    """Translate one bar term into DSL predicate text. Returns ``(text, term)``.

    This function decides **syntax** only — which field, which operator, is it
    negated. Whether the field exists, whether the operator suits it, and whether
    the value is legal are all the DSL's to answer, and are answered by it.
    """
    match = _TERM_RE.match(term)
    if match is None:
        raise _fail(
            f"cannot parse filter term {term!r}; expected <field>:<value>, "
            "-<field>:<value> to exclude, or <metric>:<op><number>"
        )

    field = match.group("field")
    op = match.group("op")
    value = match.group("value").strip()
    negate = bool(match.group("negate"))

    if not value:
        raise _fail(f"filter term {term!r} has no value")

    # Two refusals the DSL cannot make on its own, because by the time it sees
    # the predicate the intent is gone.
    if field in NUMERIC_METRICS:
        raise _fail(
            f"{field!r} is a metric, and `nova query --where` filters dimensions "
            f"only — a metric is something the DSL aggregates, not something it "
            f"filters by. Accepting {term!r} here would make this bar a second "
            f"query surface the CLI cannot reproduce. Filterable: "
            f"{', '.join(DIMENSIONS)}."
        )
    if any(ch in value for ch in _PATTERN_CHARS):
        # The DSL would compare this literally and match nothing, and the user
        # would read "no results" as "no such value". Naming the ceiling is the
        # only answer that teaches rather than misleads.
        raise _fail(
            f"{term!r} looks like a pattern, and this filter has no wildcard "
            "operator — its power ceiling is deliberately `nova query`'s, whose "
            "operators are = != < <= > >=. Use an exact value, or filter more "
            "broadly and narrow with a second term."
        )
    if negate and op == "!=":
        raise _fail(f"{term!r} negates twice; use one of '-' or '!='")
    if negate and op in (">", ">=", "<", "<="):
        # `-cost:>5` reads as "not greater than 5", which is `<=` — but guessing
        # which inversion the author meant is exactly the kind of helpfulness
        # that produces a filter they did not write. Ask them to write it.
        raise _fail(
            f"cannot combine exclusion with {op!r} in {term!r}; "
            "write the opposite comparison instead"
        )

    operator = op or "="
    if negate:
        operator = "!="
    quoted = f'"{value}"' if " " in value else value
    return f"{field} {operator} {quoted}", term


@dataclass(frozen=True)
class ObservedValues:
    """Suggestions for one dimension, and whether the list is the whole story.

    ADR-0232 D3: *"A silently truncated suggestion list teaches users that a value
    does not exist."* ``truncated`` is therefore not decoration — it is the
    difference between "these are the models" and "these are some of the models".
    """

    dimension: str
    values: tuple[str, ...]
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "values": list(self.values),
            "truncated": self.truncated,
        }


def observed_values(
    dimension: str,
    rows: Any,
    *,
    limit: int = MAX_SUGGESTIONS,
) -> ObservedValues:
    """Distinct observed values for *dimension* across indexed *rows*, bounded.

    Suggests what is actually present rather than a static list — the difference
    between an autocomplete that helps and one that lies about what exists.

    An unknown dimension **raises** rather than returning an empty list: empty
    means "this dimension has no values here", which is a different claim, and
    conflating them would let a typo read as evidence.
    """
    if dimension not in DIMENSIONS:
        raise _fail(
            f"unknown dimension {dimension!r}; allowed: {', '.join(DIMENSIONS)}"
        )
    if limit < 1:
        raise _fail("limit must be at least 1")

    seen: set[str] = set()
    truncated = False
    for row in rows:
        value = getattr(row, dimension, None)
        if value is None:
            continue
        text = str(value)
        if text in seen:
            continue
        if len(seen) >= limit:
            # Stop scanning: the answer is already "more than you asked for", and
            # continuing would be the unbounded scan this cap exists to prevent.
            truncated = True
            break
        seen.add(text)
    return ObservedValues(
        dimension=dimension, values=tuple(sorted(seen)), truncated=truncated
    )
