"""Cross-vendor entity resolution for the SPKG (ADR-0111 D4, spike SP-3).

Links the same logical entity (agent / tool / model / dataset) across telemetry from
different vendors that use different identifier strings — so the provenance graph has one
node per real entity, not one per vendor spelling. This is the scalable tier of the
three-tier ER direction (KG-ADR-003).

Implementation note (ADR-0024 / license): Splink was evaluated and REJECTED as a runtime
dependency because Splink 4.x hard-depends on **igraph (GPL-2.0, Tier C)**. This module is
a self-contained probabilistic (Fellegi-Sunter) linker built only on the Python standard
library (``difflib`` for string similarity, a union-find for clustering) — zero non-Tier-A
dependencies. DuckDB (already a NovaFabric dep, Apache-2.0/MIT) can back the blocking step
at cluster scale; the in-memory blocking here is equivalent for the local tier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from math import log2
from typing import Any, Iterable

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(name: str) -> str:
    """Vendor-agnostic normal form: lowercase, strip all non-alphanumerics."""
    return _NON_ALNUM.sub("", name.lower())


# Fellegi-Sunter comparison levels for the name field: (predicate, m, u).
# m = P(level | true match); u = P(level | non-match). Ordered most→least specific.
@dataclass(frozen=True)
class _Level:
    name: str
    m: float
    u: float


_NAME_LEVELS = [
    _Level("normalized_exact", m=0.90, u=0.0005),
    _Level("high_similarity", m=0.20, u=0.010),   # difflib ratio >= 0.85
    _Level("med_similarity", m=0.06, u=0.050),    # difflib ratio >= 0.70
    _Level("no_match", m=0.02, u=0.9395),         # complement (m/u sum to ~1)
]
_HIGH, _MED = 0.85, 0.70


def _name_level(a: str, b: str) -> _Level:
    if normalize(a) == normalize(b):
        return _NAME_LEVELS[0]
    ratio = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    if ratio >= _HIGH:
        return _NAME_LEVELS[1]
    if ratio >= _MED:
        return _NAME_LEVELS[2]
    return _NAME_LEVELS[3]


@dataclass
class MatchPair:
    left_key: str
    right_key: str
    probability: float
    level: str


@dataclass
class EntityResolver:
    """Probabilistic Fellegi-Sunter linker over two vendor record sets.

    Records are dicts with at least ``kind`` and ``name``; ``vendor`` and ``id`` identify
    the source. Blocking compares only same-``kind`` records.
    """

    threshold: float = 0.5
    prior: float = 0.15  # P(a random same-kind pair is a true match)
    _levels: list[_Level] = field(default_factory=lambda: list(_NAME_LEVELS))

    def _record_key(self, rec: dict[str, Any]) -> str:
        return f"{rec.get('vendor', '?')}:{rec.get('id', rec.get('name', ''))}"

    def _pair_probability(self, a: dict[str, Any], b: dict[str, Any]) -> tuple[float, str]:
        lvl = _name_level(str(a.get("name", "")), str(b.get("name", "")))
        # Total match weight in log2 space: prior odds + this level's Bayes factor.
        weight = log2(self.prior / (1.0 - self.prior)) + log2(lvl.m / lvl.u)
        odds = 2.0**weight
        return odds / (1.0 + odds), lvl.name

    def link(
        self, records_a: Iterable[dict[str, Any]], records_b: Iterable[dict[str, Any]]
    ) -> list[MatchPair]:
        """Return cross-vendor pairs whose match probability >= threshold."""
        a_by_kind: dict[str, list[dict[str, Any]]] = {}
        for rec in records_a:
            a_by_kind.setdefault(str(rec.get("kind", "")), []).append(rec)

        pairs: list[MatchPair] = []
        for b in records_b:
            for a in a_by_kind.get(str(b.get("kind", "")), []):
                prob, level = self._pair_probability(a, b)
                if prob >= self.threshold:
                    pairs.append(
                        MatchPair(self._record_key(a), self._record_key(b), prob, level)
                    )
        return pairs

    def cluster(
        self, records_a: Iterable[dict[str, Any]], records_b: Iterable[dict[str, Any]]
    ) -> list[set[str]]:
        """Union-find clustering of records linked above threshold. Returns entity clusters."""
        ra, rb = list(records_a), list(records_b)
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: str, y: str) -> None:
            parent[find(x)] = find(y)

        for rec in ra + rb:
            find(self._record_key(rec))
        for pair in self.link(ra, rb):
            union(pair.left_key, pair.right_key)

        clusters: dict[str, set[str]] = {}
        for key in list(parent):
            clusters.setdefault(find(key), set()).add(key)
        return list(clusters.values())
