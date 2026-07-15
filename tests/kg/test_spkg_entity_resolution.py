"""SP-3 tests: cross-vendor entity resolution for the SPKG (ADR-0111, BQ-SPKG-01).

Pure-Tier-A (stdlib only) — no splink/igraph. Verifies a two-vendor capsule fixture
resolves to one entity per real thing at high F1, and that the distractor stays separate.
"""
from __future__ import annotations

from novafabric.kg.spkg.entity_resolution import EntityResolver, normalize

RECORDS_A = [
    {"vendor": "A", "id": "a1", "kind": "model", "name": "GPT-4o (2024-08-06)"},
    {"vendor": "A", "id": "a2", "kind": "tool", "name": "web_search"},
    {"vendor": "A", "id": "a3", "kind": "tool", "name": "vector-db-lookup"},
    {"vendor": "A", "id": "a4", "kind": "model", "name": "text-embedding-3-large"},
]
RECORDS_B = [
    {"vendor": "B", "id": "b1", "kind": "model", "name": "gpt-4o-2024-08-06"},
    {"vendor": "B", "id": "b2", "kind": "tool", "name": "web-search"},
    {"vendor": "B", "id": "b3", "kind": "tool", "name": "Vector DB Lookup"},
    {"vendor": "B", "id": "b4", "kind": "model", "name": "text-embedding-3-larg"},
    {"vendor": "B", "id": "b5", "kind": "model", "name": "gemini-1.5-pro"},  # distractor
]
TRUE_PAIRS = {("A:a1", "B:b1"), ("A:a2", "B:b2"), ("A:a3", "B:b3"), ("A:a4", "B:b4")}


def test_normalize_is_vendor_agnostic() -> None:
    assert normalize("GPT-4o (2024-08-06)") == normalize("gpt-4o-2024-08-06")
    assert normalize("web_search") == normalize("web-search")
    assert normalize("Vector DB Lookup") == normalize("vector-db-lookup")


def test_sp3_cross_vendor_f1() -> None:
    resolver = EntityResolver(threshold=0.5)
    predicted = {(p.left_key, p.right_key) for p in resolver.link(RECORDS_A, RECORDS_B)}

    tp = len(predicted & TRUE_PAIRS)
    fp = len(predicted - TRUE_PAIRS)
    fn = len(TRUE_PAIRS - predicted)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    assert f1 >= 0.9, f"F1={f1} predicted={predicted}"
    # On this clean fixture we expect a perfect resolution.
    assert predicted == TRUE_PAIRS


def test_sp3_clustering_one_node_per_entity() -> None:
    resolver = EntityResolver(threshold=0.5)
    clusters = resolver.cluster(RECORDS_A, RECORDS_B)

    # 4 matched (2-member, cross-vendor) clusters + 1 singleton distractor = 5.
    assert len(clusters) == 5
    matched = [c for c in clusters if len(c) == 2]
    singletons = [c for c in clusters if len(c) == 1]
    assert len(matched) == 4
    assert singletons == [{"B:b5"}]
    for c in matched:
        vendors = {key.split(":")[0] for key in c}
        assert vendors == {"A", "B"}  # each cluster spans both vendors


def test_high_similarity_typo_still_links() -> None:
    """'text-embedding-3-large' vs '...larg' (typo) links via the high-similarity level."""
    resolver = EntityResolver(threshold=0.5)
    pairs = resolver.link(
        [RECORDS_A[3]], [RECORDS_B[3]]
    )
    assert len(pairs) == 1
    assert pairs[0].level in {"normalized_exact", "high_similarity"}


def test_distractor_does_not_match() -> None:
    resolver = EntityResolver(threshold=0.5)
    pairs = resolver.link(RECORDS_A, [RECORDS_B[4]])  # only gemini
    assert pairs == []
