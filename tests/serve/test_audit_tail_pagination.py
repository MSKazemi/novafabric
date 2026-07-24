"""Bounded tail reads + byte-offset pagination for the dashboard audit log.

ADR-0199 (serve pagination hardening): `read_recent_tail` reads the
append-only JSONL backwards from EOF in fixed-size blocks, so `/api/audit`
IO is O(page) rather than O(file). Byte offsets are stable cursors because
the file is append-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from novafabric.serve import audit
from novafabric.serve.app import create_app
from novafabric.serve.audit import read_recent, read_recent_tail, tail_lines
from novafabric.serve.pagination import (
    clamp_limit,
    decode_keyset,
    encode_keyset,
    page_envelope,
)

VALID_TOKEN = "tail-test-token-1234567890"
HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    """Isolated tmp paths for capsules, registry, and the audit log."""
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    db = tmp_path / "registry.db"
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.AUDIT_ENV, str(audit_path))
    yield {"capsule_dir": capsule_dir, "db": db, "audit": audit_path}


@pytest.fixture
def client(env: dict[str, Path]) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=env["capsule_dir"],
        db_path=env["db"],
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


def _seed(n: int, action: str = "register_asset") -> None:
    for i in range(n):
        audit.append(
            action=action,
            args={"i": i},
            cli_equivalent=f"nova x {i}",
            actor_token_fp="deadbeef",
        )


# ---------- tail_lines (low-level reverse block reader) ----------


def test_tail_lines_multi_block_yields_every_line_newest_first(tmp_path: Path) -> None:
    """Lines straddling block boundaries are reassembled; order is reversed."""
    p = tmp_path / "log.jsonl"
    # Long-ish lines + a tiny block size force many straddling boundaries.
    lines = [json.dumps({"i": i, "pad": "x" * 57}) for i in range(200)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    got = [raw.decode() for _off, raw in tail_lines(p, block_size=64)]
    assert got == list(reversed(lines))


def test_tail_lines_offsets_are_true_byte_positions(tmp_path: Path) -> None:
    p = tmp_path / "log.jsonl"
    lines = [json.dumps({"i": i}) for i in range(50)]
    text = "\n".join(lines) + "\n"
    p.write_text(text, encoding="utf-8")

    data = text.encode()
    for off, raw in tail_lines(p, block_size=16):
        assert data[off : off + len(raw)] == raw


def test_tail_lines_missing_file_yields_nothing(tmp_path: Path) -> None:
    assert list(tail_lines(tmp_path / "nope.jsonl")) == []


def test_tail_lines_skips_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "log.jsonl"
    p.write_text('{"a":1}\n\n   \n{"a":2}\n', encoding="utf-8")
    got = [raw.strip() for _off, raw in tail_lines(p, block_size=4)]
    assert got == [b'{"a":2}', b'{"a":1}']


# ---------- read_recent_tail ----------


def test_read_recent_tail_newest_first_and_exhausted(env: dict[str, Path]) -> None:
    _seed(5)
    entries, cursor = read_recent_tail(10)
    assert [e["args"]["i"] for e in entries] == [4, 3, 2, 1, 0]
    assert cursor is None  # file fully consumed — no next page


def test_read_recent_tail_cursor_round_trip(env: dict[str, Path]) -> None:
    """Paging with the returned byte-offset cursor sees every entry exactly once."""
    _seed(23)
    seen: list[int] = []
    cursor: int | None = None
    pages = 0
    while True:
        entries, cursor = read_recent_tail(5, before_offset=cursor)
        seen.extend(e["args"]["i"] for e in entries)
        pages += 1
        if cursor is None:
            break
    assert seen == list(range(22, -1, -1))  # newest-first, no dupes, no gaps
    assert pages >= 5


def test_read_recent_tail_cursor_stable_across_appends(env: dict[str, Path]) -> None:
    """Appends after a page was fetched do not shift the older pages."""
    _seed(6)
    page1, cursor = read_recent_tail(3)
    _seed(4, action="promote_asset")  # concurrent writer appends
    assert cursor is not None
    page2, _ = read_recent_tail(3, before_offset=cursor)
    assert [e["args"]["i"] for e in page1] == [5, 4, 3]
    assert [e["args"]["i"] for e in page2] == [2, 1, 0]


def test_read_recent_tail_action_filter(env: dict[str, Path]) -> None:
    _seed(3, action="register_asset")
    _seed(2, action="promote_asset")
    entries, cursor = read_recent_tail(10, action="promote_asset")
    assert [e["action"] for e in entries] == ["promote_asset"] * 2
    assert cursor is None
    entries, _ = read_recent_tail(10, action="no_such_action")
    assert entries == []


def test_read_recent_tail_skips_malformed_lines(env: dict[str, Path]) -> None:
    _seed(2)
    with env["audit"].open("a", encoding="utf-8") as f:
        f.write("{not json at all\n")
        f.write('"a bare string, not an object"\n')
    _seed(1)
    entries, cursor = read_recent_tail(10)
    assert [e["args"]["i"] for e in entries] == [0, 1, 0][::-1]
    assert cursor is None


def test_read_recent_tail_empty_and_missing_file(env: dict[str, Path]) -> None:
    assert read_recent_tail(10) == ([], None)  # missing file
    env["audit"].write_text("", encoding="utf-8")
    assert read_recent_tail(10) == ([], None)  # empty file


def test_read_recent_tail_multi_block_file(env: dict[str, Path]) -> None:
    """A file larger than one 64 KiB block pages correctly across blocks."""
    _seed(600)  # ~600 × ~130 bytes ≈ 78 KiB > TAIL_BLOCK_SIZE
    assert env["audit"].stat().st_size > audit.TAIL_BLOCK_SIZE
    entries, cursor = read_recent_tail(500)
    assert len(entries) == 500
    assert entries[0]["args"]["i"] == 599
    assert cursor is not None
    rest, cursor = read_recent_tail(500, before_offset=cursor)
    assert [e["args"]["i"] for e in rest] == list(range(99, -1, -1))
    assert cursor is None


def test_read_recent_is_thin_wrapper(env: dict[str, Path]) -> None:
    _seed(7)
    assert read_recent(3) == read_recent_tail(3)[0]


# ---------- GET /api/audit (cursor + action query params) ----------


def test_api_audit_backward_compatible_shape(client: TestClient) -> None:
    _seed(2)
    res = client.get(f"/api/audit?token={VALID_TOKEN}", headers=HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 2
    assert len(data["entries"]) == 2
    assert data["next_cursor"] is None


def test_api_audit_cursor_pagination(client: TestClient) -> None:
    _seed(7)
    res = client.get(
        f"/api/audit?token={VALID_TOKEN}&limit=4", headers=HEADERS
    )
    data = res.json()
    assert [e["args"]["i"] for e in data["entries"]] == [6, 5, 4, 3]
    assert isinstance(data["next_cursor"], int)
    res = client.get(
        f"/api/audit?token={VALID_TOKEN}&limit=4&cursor={data['next_cursor']}",
        headers=HEADERS,
    )
    data = res.json()
    assert [e["args"]["i"] for e in data["entries"]] == [2, 1, 0]
    assert data["next_cursor"] is None


def test_api_audit_action_filter(client: TestClient) -> None:
    _seed(3, action="register_asset")
    _seed(1, action="promote_asset")
    res = client.get(
        f"/api/audit?token={VALID_TOKEN}&action=register_asset", headers=HEADERS
    )
    data = res.json()
    assert data["count"] == 3
    assert all(e["action"] == "register_asset" for e in data["entries"])


def test_api_audit_rejects_bad_cursor(client: TestClient) -> None:
    res = client.get(
        f"/api/audit?token={VALID_TOKEN}&cursor=-1", headers=HEADERS
    )
    assert res.status_code == 422
    res = client.get(
        f"/api/audit?token={VALID_TOKEN}&cursor=notanint", headers=HEADERS
    )
    assert res.status_code == 422


def test_api_audit_cursor_past_eof_clamps(client: TestClient) -> None:
    _seed(2)
    res = client.get(
        f"/api/audit?token={VALID_TOKEN}&cursor=99999999", headers=HEADERS
    )
    assert res.status_code == 200
    assert res.json()["count"] == 2


# ---------- serve.pagination shared helpers ----------


def test_keyset_codec_round_trip() -> None:
    cur = encode_keyset("2026-07-24T00:00:00Z", "run-1")
    assert decode_keyset(cur) == ("2026-07-24T00:00:00Z", "run-1")
    assert decode_keyset(None) is None
    assert decode_keyset("") is None
    assert decode_keyset("!!!not-base64!!!") is None


def test_clamp_limit() -> None:
    assert clamp_limit(0, 100) == 1
    assert clamp_limit(50, 100) == 50
    assert clamp_limit(101, 100) == 100
    assert clamp_limit(-7, 100) == 1


def test_page_envelope_shape() -> None:
    assert page_envelope([1, 2]) == {
        "items": [1, 2],
        "next_cursor": None,
        "total": None,
        "truncated": False,
    }
    env = page_envelope([], next_cursor="c", total=9, truncated=True)
    assert env == {"items": [], "next_cursor": "c", "total": 9, "truncated": True}
