"""Tests for AliasTableResolver (Tier-2 entity resolution, SQLite-backed).

All tests use a tmp_path fixture — no Postgres / asyncpg required.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolver(tmp_path: Path, cache_ttl: int = 300):
    from novafabric.kg.alias_resolver import AliasTableResolver

    return AliasTableResolver(db_path=tmp_path / "alias.db", cache_ttl_seconds=cache_ttl)


def _entry(alias: str, canonical: str, entity_type: str = "model", **kw):
    from novafabric.kg.alias_resolver import AliasEntry

    return AliasEntry(
        alias=alias,
        canonical=canonical,
        entity_type=entity_type,
        created_at=datetime.now(timezone.utc),
        **kw,
    )


# ---------------------------------------------------------------------------
# resolve() — cache miss
# ---------------------------------------------------------------------------


def test_resolve_miss_returns_none(tmp_path: Path) -> None:
    """resolve() returns None for an unknown alias."""
    r = _resolver(tmp_path)
    assert r.resolve("unknown-model-xyz", "model") is None
    r.close()


# ---------------------------------------------------------------------------
# register() + resolve() — exact match
# ---------------------------------------------------------------------------


def test_resolve_after_register(tmp_path: Path) -> None:
    """resolve() returns canonical after register()."""
    r = _resolver(tmp_path)
    r.register(_entry("gpt4", "gpt-4o"))
    assert r.resolve("gpt4", "model") == "gpt-4o"
    r.close()


def test_resolve_cache_hit_after_first_lookup(tmp_path: Path) -> None:
    """Second resolve() for same key uses in-memory cache (no SQLite re-query)."""
    r = _resolver(tmp_path)
    r.register(_entry("myalias", "canonical-name"))
    # first call populates cache
    result1 = r.resolve("myalias", "model")
    # manually corrupt the DB to verify cache is used
    r._conn.execute(
        "UPDATE alias_table SET canonical = 'wrong' WHERE alias = 'myalias'"
    )
    result2 = r.resolve("myalias", "model")
    assert result1 == "canonical-name"
    assert result2 == "canonical-name"  # served from cache
    r.close()


def test_resolve_cache_miss_after_ttl_expiry(tmp_path: Path) -> None:
    """resolve() re-queries SQLite after TTL expiry."""
    r = _resolver(tmp_path, cache_ttl=0)  # instant expiry
    r.register(_entry("shortlived", "v1"))
    # first call
    r.resolve("shortlived", "model")
    # update DB
    r._conn.execute(
        "UPDATE alias_table SET canonical = 'v2' WHERE alias = 'shortlived'"
    )
    r._conn.commit()
    # cache expired → should re-query
    result = r.resolve("shortlived", "model")
    assert result == "v2"
    r.close()


# ---------------------------------------------------------------------------
# heuristic version-stripping
# ---------------------------------------------------------------------------


def test_heuristic_date_suffix_resolves(tmp_path: Path) -> None:
    """gpt-4o-2024-05-13 → looks up gpt-4o via date-suffix stripping."""
    r = _resolver(tmp_path)
    r.register(_entry("gpt-4o", "gpt-4o"))
    result = r.resolve("gpt-4o-2024-05-13", "model")
    assert result == "gpt-4o"
    r.close()


def test_heuristic_v_suffix_resolves(tmp_path: Path) -> None:
    """mymodel-v3 → looks up mymodel via -v<digits> stripping."""
    r = _resolver(tmp_path)
    r.register(_entry("mymodel", "mymodel-canonical"))
    result = r.resolve("mymodel-v3", "model")
    assert result == "mymodel-canonical"
    r.close()


def test_heuristic_vendor_prefix_resolves(tmp_path: Path) -> None:
    """openai/gpt-4o → looks up gpt-4o via vendor-prefix stripping."""
    r = _resolver(tmp_path)
    r.register(_entry("gpt-4o", "gpt-4o"))
    result = r.resolve("openai/gpt-4o", "model")
    assert result == "gpt-4o"
    r.close()


def test_heuristic_no_strip_match_returns_none(tmp_path: Path) -> None:
    """Stripping doesn't help → still returns None."""
    r = _resolver(tmp_path)
    result = r.resolve("completely-unknown-v5", "model")
    assert result is None
    r.close()


# ---------------------------------------------------------------------------
# entity_type isolation
# ---------------------------------------------------------------------------


def test_entity_type_isolation(tmp_path: Path) -> None:
    """Same alias with different entity_types resolve independently."""
    r = _resolver(tmp_path)
    r.register(_entry("gpt-4o", "gpt-4o-model", entity_type="model"))
    r.register(_entry("gpt-4o", "gpt-4o-tool", entity_type="tool"))
    assert r.resolve("gpt-4o", "model") == "gpt-4o-model"
    assert r.resolve("gpt-4o", "tool") == "gpt-4o-tool"
    r.close()


# ---------------------------------------------------------------------------
# bulk_register()
# ---------------------------------------------------------------------------


def test_bulk_register(tmp_path: Path) -> None:
    """bulk_register inserts all entries and they all resolve."""

    r = _resolver(tmp_path)
    entries = [
        _entry(f"alias-{i}", f"canonical-{i}") for i in range(5)
    ]
    r.bulk_register(entries)
    for i in range(5):
        assert r.resolve(f"alias-{i}", "model") == f"canonical-{i}"
    r.close()


def test_bulk_register_empty_is_noop(tmp_path: Path) -> None:
    """bulk_register([]) does not raise."""
    r = _resolver(tmp_path)
    r.bulk_register([])  # must not raise
    r.close()


# ---------------------------------------------------------------------------
# list_aliases()
# ---------------------------------------------------------------------------


def test_list_aliases_empty(tmp_path: Path) -> None:
    """list_aliases returns [] when nothing registered."""
    r = _resolver(tmp_path)
    assert r.list_aliases("nobody") == []
    r.close()


def test_list_aliases_returns_all_for_canonical(tmp_path: Path) -> None:
    """list_aliases returns all entries mapped to the same canonical."""
    r = _resolver(tmp_path)
    r.register(_entry("gpt4", "gpt-4o"))
    r.register(_entry("gpt-4o-mini", "gpt-4o-mini"))  # different canonical
    r.register(_entry("openai-gpt4o", "gpt-4o"))
    aliases = r.list_aliases("gpt-4o")
    assert len(aliases) == 2
    alias_names = {e.alias for e in aliases}
    assert "gpt4" in alias_names
    assert "openai-gpt4o" in alias_names
    r.close()


# ---------------------------------------------------------------------------
# clear_cache()
# ---------------------------------------------------------------------------


def test_clear_cache(tmp_path: Path) -> None:
    """clear_cache() empties the in-memory dict."""
    r = _resolver(tmp_path)
    r.register(_entry("a", "b"))
    r.resolve("a", "model")  # populate cache
    assert len(r._cache) > 0
    r.clear_cache()
    assert len(r._cache) == 0
    r.close()


# ---------------------------------------------------------------------------
# register() overwrites existing entry
# ---------------------------------------------------------------------------


def test_register_overwrites_existing(tmp_path: Path) -> None:
    """Second register() for same (alias, entity_type) updates canonical."""
    r = _resolver(tmp_path)
    r.register(_entry("model-x", "old-canonical"))
    r.clear_cache()
    r.register(_entry("model-x", "new-canonical"))
    assert r.resolve("model-x", "model") == "new-canonical"
    r.close()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_register_and_resolve(tmp_path: Path) -> None:
    """Concurrent register + resolve from multiple threads is race-free."""
    r = _resolver(tmp_path)
    errors: list[str] = []

    def writer(n: int) -> None:
        try:
            r.register(_entry(f"alias-thread-{n}", f"canonical-{n}"))
        except Exception as exc:
            errors.append(str(exc))

    def reader(n: int) -> None:
        try:
            r.resolve(f"alias-thread-{n}", "model")
        except Exception as exc:
            errors.append(str(exc))

    threads = [
        threading.Thread(target=writer, args=(i,)) for i in range(10)
    ] + [
        threading.Thread(target=reader, args=(i,)) for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    r.close()


# ---------------------------------------------------------------------------
# Persistence across instances
# ---------------------------------------------------------------------------


def test_persistence_across_instances(tmp_path: Path) -> None:
    """Aliases survive closing and re-opening the resolver (SQLite persistence)."""
    from novafabric.kg.alias_resolver import AliasTableResolver

    db = tmp_path / "alias.db"
    r1 = AliasTableResolver(db_path=db)
    r1.register(_entry("persistent-alias", "persistent-canonical"))
    r1.close()

    r2 = AliasTableResolver(db_path=db)
    assert r2.resolve("persistent-alias", "model") == "persistent-canonical"
    r2.close()
