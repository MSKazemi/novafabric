"""ADR-0122 P3: the local SQLite session index is a rebuildable, fail-safe cache.

Acceptance criteria under test:

- a fresh index serves ``list`` without parsing manifests, with output equal
  to the authoritative directory scan (same manifests, same newest-first order);
- missing / stale / corrupt / wrong-version index → directory-scan fallback,
  never an error, with the reason reported;
- ``save_session`` refreshes an existing index (write-through) but never
  creates one; an index failure never fails a save;
- ``rebuild_index`` recovers from a corrupt file and records unreadable
  manifests without listing them;
- concurrent writers do not corrupt the index (WAL + busy_timeout).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from novafabric.session import (
    SESSION_INDEX_FILENAME,
    SessionIndexError,
    list_sessions,
    list_sessions_fast,
    new_session,
    rebuild_index,
    save_session,
)
from novafabric.session import index as index_mod


def _make_sessions(root: Path, n: int) -> list[str]:
    ids = []
    for i in range(n):
        manifest = new_session(kind="workflow" if i % 2 else "conversation")
        save_session(manifest, root=root)
        ids.append(manifest.session_id)
    return ids


def _ids(listing_manifests: list) -> list[str]:  # type: ignore[type-arg]
    return [m.session_id for m in listing_manifests]


class TestFreshIndex:
    def test_rebuild_then_list_is_served_from_index(self, tmp_path: Path) -> None:
        ids = _make_sessions(tmp_path, 3)
        report = rebuild_index(root=tmp_path)
        assert report.indexed == 3
        assert report.unreadable == 0
        assert Path(report.index_path) == tmp_path / SESSION_INDEX_FILENAME

        listing = list_sessions_fast(root=tmp_path)
        assert listing.source == "index"
        assert listing.index_status == "fresh"
        assert listing.detail is None
        assert _ids(listing.manifests) == sorted(ids, reverse=True)
        # identical to the authoritative scan
        assert [m.to_json_dict() for m in listing.manifests] == [
            m.to_json_dict() for m in list_sessions(root=tmp_path)
        ]

    def test_fresh_index_does_not_parse_manifests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_sessions(tmp_path, 2)
        rebuild_index(root=tmp_path)

        def boom(path: Path) -> None:
            raise AssertionError(f"manifest parsed on the fast path: {path}")

        monkeypatch.setattr(index_mod, "load_manifest_file", boom)
        monkeypatch.setattr(index_mod, "list_sessions", boom)
        assert list_sessions_fast(root=tmp_path).source == "index"

    def test_empty_root_rebuild_and_list(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        assert rebuild_index(root=root).indexed == 0
        listing = list_sessions_fast(root=root)
        assert listing.source == "index"
        assert listing.manifests == []

    def test_write_through_keeps_index_fresh(self, tmp_path: Path) -> None:
        _make_sessions(tmp_path, 1)
        rebuild_index(root=tmp_path)
        (new_id,) = _make_sessions(tmp_path, 1)  # save_session upserts
        listing = list_sessions_fast(root=tmp_path)
        assert listing.source == "index"
        assert new_id in _ids(listing.manifests)


class TestFallback:
    def test_missing_index_falls_back_to_scan(self, tmp_path: Path) -> None:
        ids = _make_sessions(tmp_path, 2)
        listing = list_sessions_fast(root=tmp_path)
        assert listing.source == "scan"
        assert listing.index_status == "missing"
        assert _ids(listing.manifests) == sorted(ids, reverse=True)
        # save_session never creates an index on its own
        assert not (tmp_path / SESSION_INDEX_FILENAME).exists()

    def test_nonexistent_root(self, tmp_path: Path) -> None:
        listing = list_sessions_fast(root=tmp_path / "nope")
        assert listing.index_status == "missing"
        assert listing.manifests == []

    def test_manifest_edited_behind_the_index_is_stale(self, tmp_path: Path) -> None:
        (sid,) = _make_sessions(tmp_path, 1)
        rebuild_index(root=tmp_path)
        path = tmp_path / sid / "session.json"
        data = json.loads(path.read_text())
        data["user_ref"] = "user:edited-externally"
        path.write_text(json.dumps(data, indent=2) + "\n")

        listing = list_sessions_fast(root=tmp_path)
        assert listing.source == "scan"
        assert listing.index_status == "stale"
        assert sid in (listing.detail or "")
        assert listing.manifests[0].user_ref == "user:edited-externally"

    def test_session_added_behind_the_index_is_stale(self, tmp_path: Path) -> None:
        _make_sessions(tmp_path, 1)
        rebuild_index(root=tmp_path)
        # A manifest dropped in by hand (not through save_session).
        other = tmp_path / "other"
        _make_sessions(other, 1)
        (moved,) = [p for p in other.iterdir() if p.is_dir()]
        moved.rename(tmp_path / moved.name)
        listing = list_sessions_fast(root=tmp_path)
        assert listing.index_status == "stale"
        assert len(listing.manifests) == 2

    def test_many_stale_sessions_detail_is_truncated(self, tmp_path: Path) -> None:
        rebuild_index(root=tmp_path)
        other = tmp_path / "other"
        _make_sessions(other, 4)
        for p in [p for p in other.iterdir() if p.is_dir()]:
            p.rename(tmp_path / p.name)
        listing = list_sessions_fast(root=tmp_path)
        assert listing.index_status == "stale"
        assert "4 session(s)" in (listing.detail or "")
        assert (listing.detail or "").endswith("…")

    def test_session_deleted_behind_the_index_is_stale(self, tmp_path: Path) -> None:
        ids = _make_sessions(tmp_path, 2)
        rebuild_index(root=tmp_path)
        (tmp_path / ids[0] / "session.json").unlink()
        listing = list_sessions_fast(root=tmp_path)
        assert listing.index_status == "stale"
        assert _ids(listing.manifests) == [ids[1]]

    def test_corrupt_index_falls_back(self, tmp_path: Path) -> None:
        ids = _make_sessions(tmp_path, 2)
        (tmp_path / SESSION_INDEX_FILENAME).write_bytes(b"this is not sqlite" * 100)
        listing = list_sessions_fast(root=tmp_path)
        assert listing.source == "scan"
        assert listing.index_status == "corrupt"
        assert sorted(_ids(listing.manifests)) == sorted(ids)

    def test_undecodable_row_falls_back(self, tmp_path: Path) -> None:
        (sid,) = _make_sessions(tmp_path, 1)
        rebuild_index(root=tmp_path)
        with sqlite3.connect(tmp_path / SESSION_INDEX_FILENAME) as conn:
            conn.execute("UPDATE sessions SET manifest_json = '{\"bad\": 1}'")
        listing = list_sessions_fast(root=tmp_path)
        assert listing.index_status == "corrupt"
        assert _ids(listing.manifests) == [sid]

    def test_version_mismatch_falls_back_and_rebuild_recovers(self, tmp_path: Path) -> None:
        _make_sessions(tmp_path, 1)
        rebuild_index(root=tmp_path)
        with sqlite3.connect(tmp_path / SESSION_INDEX_FILENAME) as conn:
            conn.execute("PRAGMA user_version=99")
        listing = list_sessions_fast(root=tmp_path)
        assert listing.index_status == "version_mismatch"
        assert listing.source == "scan"
        # write-through refuses to touch a foreign-version index
        _make_sessions(tmp_path, 1)
        rebuild_index(root=tmp_path)
        assert list_sessions_fast(root=tmp_path).index_status == "fresh"

    def test_rebuild_recovers_a_corrupt_file(self, tmp_path: Path) -> None:
        _make_sessions(tmp_path, 2)
        (tmp_path / SESSION_INDEX_FILENAME).write_bytes(b"garbage" * 500)
        assert rebuild_index(root=tmp_path).indexed == 2
        assert list_sessions_fast(root=tmp_path).source == "index"


class TestUnreadableAndFailures:
    def test_unreadable_manifest_recorded_but_not_listed(self, tmp_path: Path) -> None:
        (good,) = _make_sessions(tmp_path, 1)
        bad = tmp_path / "01HZZZZZZZZZZZZZZZZZZZZZZZ"
        bad.mkdir()
        (bad / "session.json").write_text("{not json")
        report = rebuild_index(root=tmp_path)
        assert (report.indexed, report.unreadable) == (1, 1)
        listing = list_sessions_fast(root=tmp_path)
        assert listing.source == "index"  # unreadable row still counts as fresh
        assert _ids(listing.manifests) == [good]

    def test_save_survives_an_unwritable_index(self, tmp_path: Path) -> None:
        _make_sessions(tmp_path, 1)
        (tmp_path / SESSION_INDEX_FILENAME).write_bytes(b"corrupt" * 200)
        # write-through fails (not a database) but the save must succeed
        (sid,) = _make_sessions(tmp_path, 1)
        assert (tmp_path / sid / "session.json").is_file()

    def test_upsert_without_index_or_manifest_is_a_noop(self, tmp_path: Path) -> None:
        assert index_mod.upsert_session("nope", root=tmp_path) is False
        rebuild_index(root=tmp_path)
        assert index_mod.upsert_session("nope", root=tmp_path) is False

    def test_upsert_rolls_back_on_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (sid,) = _make_sessions(tmp_path, 1)
        rebuild_index(root=tmp_path)
        # A row of the wrong arity fails the INSERT inside the transaction.
        monkeypatch.setattr(index_mod, "_row_for", lambda base, name: ("x",))
        assert index_mod.upsert_session(sid, root=tmp_path) is False
        assert list_sessions_fast(root=tmp_path).index_status == "fresh"

    def test_rebuild_failure_is_a_named_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_sessions(tmp_path, 1)
        monkeypatch.setattr(index_mod, "_row_for", lambda base, name: ("x",))
        with pytest.raises(SessionIndexError, match="cannot rebuild"):
            rebuild_index(root=tmp_path)

    def test_disk_state_ignores_files_and_symlinks(self, tmp_path: Path) -> None:
        (sid,) = _make_sessions(tmp_path, 1)
        (tmp_path / "stray.txt").write_text("x")
        (tmp_path / "link").symlink_to(tmp_path / sid)
        (tmp_path / "empty-dir").mkdir()
        assert set(index_mod._disk_state(tmp_path)) == {sid}
        assert index_mod._stat_key(tmp_path / "empty-dir") is None
        assert index_mod._disk_state(tmp_path / "absent") == {}


def test_concurrent_saves_keep_index_consistent(tmp_path: Path) -> None:
    rebuild_index(root=tmp_path)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            _make_sessions(tmp_path, 5)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    listing = list_sessions_fast(root=tmp_path)
    assert len(listing.manifests) == 20
    assert listing.index_status == "fresh", listing.detail


class TestScanAndIndexAgree:
    def test_symlinked_session_dir_is_excluded_by_both(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        (sid,) = _make_sessions(root, 1)
        elsewhere = tmp_path / "elsewhere"
        (other,) = _make_sessions(elsewhere, 1)
        (root / other).symlink_to(elsewhere / other)  # a session dir that is a link
        (root / "alias").symlink_to(root / sid)  # an alias of a real session
        rebuild_index(root=root)
        scan = list_sessions(root=root)
        listing = list_sessions_fast(root=root)
        assert listing.index_status == "fresh", listing.detail
        assert _ids(listing.manifests) == _ids(scan) == [sid]


class TestReadsNeverCreate:
    def test_index_vanishing_before_open_is_missing_and_not_recreated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (sid,) = _make_sessions(tmp_path, 1)
        rebuild_index(root=tmp_path)
        path = tmp_path / SESSION_INDEX_FILENAME
        real = index_mod._read_index

        def vanish_then_read(p: Path):  # type: ignore[no-untyped-def]
            index_mod._discard(p)  # removed between is_file() and connect
            return real(p)

        monkeypatch.setattr(index_mod, "_read_index", vanish_then_read)
        listing = list_sessions_fast(root=tmp_path)
        assert listing.source == "scan"
        assert listing.index_status == "missing", listing.detail
        assert _ids(listing.manifests) == [sid]
        assert not path.exists()  # a read never creates the database

    def test_read_opens_the_index_read_only(self, tmp_path: Path) -> None:
        _make_sessions(tmp_path, 1)
        rebuild_index(root=tmp_path)
        path = tmp_path / SESSION_INDEX_FILENAME
        with index_mod._connect_ro(path) as conn, pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM sessions")
        with pytest.raises(sqlite3.OperationalError):
            with index_mod._connect_ro(tmp_path / "absent.sqlite"):
                pass  # pragma: no cover - connect itself raises
        assert not (tmp_path / "absent.sqlite").exists()
