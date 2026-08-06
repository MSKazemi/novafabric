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

"""The persistent `nova query` index is a speed-up that cannot change an answer (ADR-0225).

The cache's entire contract is negative: it must never make a query wrong. So
these tests are mostly about the ways it could — a capsule that changed without
the cache noticing, a cache that outlived its schema, a row whose capsule is
gone, a second process holding the write lock.

The equivalence check is the spine: for every mutation, the cached answer must
equal the answer a full scan gives. It is asserted against the **uncached** path
rather than against a hand-written expectation, so the test cannot drift from
the indexer it is meant to shadow.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.query.cache import (
    CACHE_SCHEMA_VERSION,
    cache_db_path,
    capsule_signature,
    scan_capsule_dir_cached,
)
from novafabric.query.indexer import scan_capsule_dir

_CARD = "sha256:" + "60" * 32
_SUBJECT = "sha256:" + "9f" * 32


def _write_capsule(
    base: Path,
    run_id: str,
    *,
    calls: list[dict[str, Any]] | None = None,
    manifest_extra: dict[str, Any] | None = None,
) -> Path:
    cdir = base / run_id
    cdir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": "2026-08-06T10:00:00+00:00",
        "status": "success",
    }
    manifest.update(manifest_extra or {})
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    if calls:
        (cdir / "model-calls.jsonl").write_text(
            "\n".join(json.dumps(c) for c in calls) + "\n"
        )
    return cdir


def _append_score(cdir: Path, name: str, value: float) -> None:
    """Append one score the way the product does — open("a"), never a rewrite."""
    from novafabric.capture._ulid import new_ulid

    record = {
        "score_id": new_ulid(),
        "name": name,
        "value": value,
        "value_type": "numeric",
        "source": "code",
        "evaluator_id": "test://evaluator",
        "subject": _SUBJECT,
        "subject_kind": "capsule",
        "eval_card_digest": _CARD,
        "created_at": "2026-08-06T10:00:00+00:00",
    }
    with (cdir / "scores.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _rows_as_tuples(rows: Any) -> tuple[Any, ...]:
    return (
        tuple(sorted(map(repr, rows.calls))),
        tuple(sorted(map(repr, rows.scores))),
        rows.capsule_count,
    )


@pytest.fixture
def capsules(tmp_path: Path) -> Path:
    base = tmp_path / "capsules"
    base.mkdir()
    _write_capsule(
        base,
        "run-a",
        calls=[{"gen_ai.response.model": "gpt-4", "duration_ms": 10}],
    )
    _write_capsule(base, "run-b", calls=[{"gen_ai.response.model": "claude", "duration_ms": 20}])
    return base


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point NOVAFABRIC_HOME at tmp_path — the cache must never touch a real home."""
    home = tmp_path / "home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    return home / "query-index.db"


def _assert_matches_full_scan(base: Path) -> None:
    """The cached answer equals the uncached answer. The whole contract, in one line."""
    assert _rows_as_tuples(scan_capsule_dir_cached(base)) == _rows_as_tuples(
        scan_capsule_dir(base)
    )


class TestEquivalence:
    def test_cold_cache_matches_full_scan(self, capsules: Path, cache: Path) -> None:
        _assert_matches_full_scan(capsules)

    def test_warm_cache_matches_full_scan(self, capsules: Path, cache: Path) -> None:
        scan_capsule_dir_cached(capsules)  # populate
        assert cache.exists(), "expected the cache to be written on the first scan"
        _assert_matches_full_scan(capsules)

    def test_cache_is_written_under_nova_home(self, capsules: Path, cache: Path) -> None:
        """D2: the cache lives outside the capsule directory, which stays read-only."""
        before = sorted(p.name for p in capsules.rglob("*"))
        scan_capsule_dir_cached(capsules)
        assert cache == cache_db_path()
        assert sorted(p.name for p in capsules.rglob("*")) == before


class TestInvalidation:
    """Every way a capsule's rows can change must be noticed."""

    def test_second_appended_score_is_seen(self, capsules: Path, cache: Path) -> None:
        """The case ADR-0225 D1's directory-mtime rule would have missed.

        The first score creates scores.jsonl, which moves the directory mtime.
        The second is an append to an existing file, which does not. A cache
        keyed on the directory alone would serve the one-score answer forever.
        """
        cdir = capsules / "run-a"
        _append_score(cdir, "quality", 1.0)
        first = scan_capsule_dir_cached(capsules)
        assert len(first.scores) == 1

        dir_mtime_before = cdir.stat().st_mtime_ns
        _append_score(cdir, "quality", 0.0)
        assert cdir.stat().st_mtime_ns == dir_mtime_before, (
            "this test is only meaningful while an append leaves the directory "
            "mtime untouched; if that changed, the ADR's original rule would suffice"
        )

        second = scan_capsule_dir_cached(capsules)
        assert len(second.scores) == 2
        _assert_matches_full_scan(capsules)

    def test_edited_manifest_is_seen(self, capsules: Path, cache: Path) -> None:
        scan_capsule_dir_cached(capsules)
        _write_capsule(capsules, "run-a", manifest_extra={"status": "failure"})
        rows = scan_capsule_dir_cached(capsules)
        assert {r.status for r in rows.calls if r.run_id == "run-a"} == {"failure"}
        _assert_matches_full_scan(capsules)

    def test_changed_model_calls_are_seen(self, capsules: Path, cache: Path) -> None:
        scan_capsule_dir_cached(capsules)
        (capsules / "run-a" / "model-calls.jsonl").write_text(
            json.dumps({"gen_ai.response.model": "gpt-5", "duration_ms": 99}) + "\n"
        )
        rows = scan_capsule_dir_cached(capsules)
        assert "gpt-5" in {r.model for r in rows.calls}
        _assert_matches_full_scan(capsules)

    def test_new_capsule_is_seen(self, capsules: Path, cache: Path) -> None:
        scan_capsule_dir_cached(capsules)
        _write_capsule(capsules, "run-c", calls=[{"gen_ai.response.model": "gemini"}])
        rows = scan_capsule_dir_cached(capsules)
        assert rows.capsule_count == 3
        _assert_matches_full_scan(capsules)

    def test_removed_capsule_is_dropped_and_pruned(
        self, capsules: Path, cache: Path
    ) -> None:
        """D3/OQ-2: a row whose source capsule is gone must never be served."""
        scan_capsule_dir_cached(capsules)
        for path in sorted((capsules / "run-b").iterdir()):
            path.unlink()
        (capsules / "run-b").rmdir()

        rows = scan_capsule_dir_cached(capsules)
        assert rows.capsule_count == 1
        assert "run-b" not in {r.run_id for r in rows.calls}
        _assert_matches_full_scan(capsules)

        with sqlite3.connect(cache) as conn:
            keys = [row[0] for row in conn.execute("SELECT capsule_key FROM capsules")]
        assert not any(key.endswith("run-b") for key in keys), (
            f"the deleted capsule's rows were left in the cache: {keys}"
        )

    def test_signature_changes_when_a_file_is_appended_to(self, capsules: Path) -> None:
        """The unit behind the above: the signature, not the directory, is the check."""
        cdir = capsules / "run-a"
        _append_score(cdir, "quality", 1.0)
        before = capsule_signature(cdir)
        time.sleep(0.01)
        _append_score(cdir, "quality", 0.0)
        assert capsule_signature(cdir) != before


class TestDegradation:
    """D3 — every failure mode ends in the right answer, never a fast wrong one."""

    def test_corrupt_cache_file_still_answers(self, capsules: Path, cache: Path) -> None:
        scan_capsule_dir_cached(capsules)
        cache.write_bytes(b"this is not a database")
        _assert_matches_full_scan(capsules)

    def test_schema_version_mismatch_discards_the_cache(
        self, capsules: Path, cache: Path
    ) -> None:
        scan_capsule_dir_cached(capsules)
        with sqlite3.connect(cache) as conn:
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'cache_schema_version'",
                (str(CACHE_SCHEMA_VERSION + 1),),
            )
            conn.commit()
        _assert_matches_full_scan(capsules)

    def test_damaged_row_falls_back_to_rescanning_that_capsule(
        self, capsules: Path, cache: Path
    ) -> None:
        scan_capsule_dir_cached(capsules)
        with sqlite3.connect(cache) as conn:
            conn.execute("UPDATE capsules SET payload = '{{{not json'")
            conn.commit()
        _assert_matches_full_scan(capsules)

    def test_unwritable_cache_location_still_answers(
        self, capsules: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("a file where a directory should be")
        monkeypatch.setenv("NOVAFABRIC_HOME", str(blocked))
        _assert_matches_full_scan(capsules)

    def test_missing_capsule_dir_still_raises(self, tmp_path: Path, cache: Path) -> None:
        """A real error stays a real error — the cache does not swallow it."""
        from novafabric.query.errors import QueryIndexError

        with pytest.raises(QueryIndexError):
            scan_capsule_dir_cached(tmp_path / "does-not-exist")


class TestConcurrency:
    def test_a_held_write_lock_does_not_block_or_fail_the_query(
        self, capsules: Path, cache: Path
    ) -> None:
        """D4: a query is a read. It must never fail because another was refreshing."""
        scan_capsule_dir_cached(capsules)  # create the db so it can be locked
        holder = sqlite3.connect(cache, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")  # hold the write lock
            _write_capsule(capsules, "run-c", calls=[{"gen_ai.response.model": "x"}])
            started = time.monotonic()
            rows = scan_capsule_dir_cached(capsules)
            elapsed = time.monotonic() - started
            assert rows.capsule_count == 3, "the new capsule must still be reported"
            assert elapsed < 2.0, f"query waited {elapsed:.2f}s on another writer's lock"
        finally:
            holder.close()


class TestControls:
    def test_no_cache_never_writes_one(self, capsules: Path, cache: Path) -> None:
        rows = scan_capsule_dir_cached(capsules, use_cache=False)
        assert _rows_as_tuples(rows) == _rows_as_tuples(scan_capsule_dir(capsules))
        assert not cache.exists(), "--no-cache must not create a cache"

    def test_rebuild_refreshes_every_capsule(self, capsules: Path, cache: Path) -> None:
        scan_capsule_dir_cached(capsules)
        # Poison the cache with rows that would be *served* on the normal path —
        # a matching signature over a wrong payload — then prove --rebuild-index
        # does not consult them.
        with sqlite3.connect(cache) as conn:
            conn.execute(
                "UPDATE capsules SET payload = '{\"calls\":[],\"scores\":[]}'"
            )
            conn.commit()
        rows = scan_capsule_dir_cached(capsules, rebuild=True)
        assert _rows_as_tuples(rows) == _rows_as_tuples(scan_capsule_dir(capsules))

    def test_rebuild_prunes_capsules_that_are_gone(
        self, capsules: Path, cache: Path
    ) -> None:
        scan_capsule_dir_cached(capsules)
        for path in sorted((capsules / "run-b").iterdir()):
            path.unlink()
        (capsules / "run-b").rmdir()
        scan_capsule_dir_cached(capsules, rebuild=True)
        with sqlite3.connect(cache) as conn:
            keys = [row[0] for row in conn.execute("SELECT capsule_key FROM capsules")]
        assert not any(key.endswith("run-b") for key in keys)


class TestQueryIntegration:
    def test_run_query_is_identical_cached_and_uncached(
        self, capsules: Path, cache: Path
    ) -> None:
        from datetime import datetime, timezone

        from novafabric.query.executor import run_query
        from novafabric.query.parser import plan_from_query_object

        _append_score(capsules / "run-a", "quality", 1.0)
        plan = plan_from_query_object({}, select="count(), avg(latency)", group_by="status")
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

        uncached = run_query(plan, capsules, now=now, use_cache=False)
        cold = run_query(plan, capsules, now=now)
        warm = run_query(plan, capsules, now=now)

        for result in (cold, warm):
            assert result["rows"] == uncached["rows"]
            assert result["row_count"] == uncached["row_count"]
