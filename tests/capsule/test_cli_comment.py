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

"""CLI smoke tests for ``nova comment add | list`` (experimental, ADR-0121)."""

from __future__ import annotations

import json
from pathlib import Path

from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.capsule.comments import COMMENTS_FILENAME, capsule_subject_digest, read_comments
from novafabric.cli.main import app

runner = CliRunner()

_SECRET = "sk-ant-" + "a" * 40


def _make_capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "trace.jsonl").write_text('{"event": "start"}\n')
    return cap


def test_comment_help() -> None:
    result = runner.invoke(app, ["comment", "--help"])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "list" in result.output


def test_add_and_list_roundtrip(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(
        app,
        ["comment", "add", "--subject", str(cap), "--body", "stale doc upstream",
         "--author", "m.ardebili", "--tag", "review"],
    )
    assert result.exit_code == 0, result.output
    assert "appended" in result.output

    listed = runner.invoke(app, ["comment", "list", "--subject", str(cap), "--json"])
    assert listed.exit_code == 0, listed.output
    records = json.loads(listed.output)
    assert len(records) == 1
    assert records[0]["body"] == "stale doc upstream"
    assert records[0]["author"] == "m.ardebili"
    assert records[0]["subject"] == capsule_subject_digest(cap)
    assert records[0]["subject_kind"] == "capsule"
    assert records[0]["tags"] == ["review"]


def test_add_with_digest_subject_requires_capsule(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    digest = capsule_subject_digest(cap)
    missing = runner.invoke(
        app, ["comment", "add", "--subject", digest, "--body", "note"]
    )
    assert missing.exit_code == 2
    assert "--capsule" in missing.output

    ok = runner.invoke(
        app,
        ["comment", "add", "--subject", digest, "--capsule", str(cap),
         "--kind", "span", "--body", "span-level note"],
    )
    assert ok.exit_code == 0, ok.output
    records = read_comments(cap / COMMENTS_FILENAME)
    assert records[0].subject_kind.value == "span"


def test_add_json_output(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(
        app, ["comment", "add", "--subject", str(cap), "--body", "note", "--json"]
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["body"] == "note"


def test_secret_body_refused_with_exit_3(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(
        app, ["comment", "add", "--subject", str(cap), "--body", f"key {_SECRET}"]
    )
    assert result.exit_code == 3
    assert _SECRET not in result.output  # the secret is never echoed
    assert not (cap / COMMENTS_FILENAME).exists()  # nothing entered the store


def test_secret_body_redacted_with_flag(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(
        app,
        ["comment", "add", "--subject", str(cap), "--body", f"key {_SECRET}", "--redact"],
    )
    assert result.exit_code == 0, result.output
    record = read_comments(cap / COMMENTS_FILENAME)[0]
    assert record.redaction_applied is True
    assert _SECRET not in record.body
    assert "[REDACTED:anthropic-api-key]" in record.body


def test_asset_subject_without_kind_asset_is_rejected(tmp_path: Path) -> None:
    """An asset:// subject must be paired with --kind asset.

    Was "planned, not implemented" until ADR-0121 P3 shipped; the remaining
    exit-2 is the model enforcing that subject and subject_kind agree, which
    is a real invariant rather than a placeholder.
    """
    result = runner.invoke(
        app,
        ["comment", "add", "--subject", "asset://prompt/triage@1.0.0", "--body", "n"],
    )
    assert result.exit_code == 2
    assert "subject_kind" in result.output or "asset" in result.output


def test_tombstone_requires_reply_to(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(
        app, ["comment", "add", "--subject", str(cap), "--body", "n", "--tombstone"]
    )
    assert result.exit_code == 2
    assert_flag_in_help(result, "--reply-to")


def test_list_hides_tombstoned_by_default(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    added = runner.invoke(
        app, ["comment", "add", "--subject", str(cap), "--body", "wrong note", "--json"]
    )
    assert added.exit_code == 0, added.output
    comment_id = json.loads(added.output)["comment_id"]
    stone = runner.invoke(
        app,
        ["comment", "add", "--subject", str(cap), "--body", "retracting: false alarm",
         "--tombstone", "--reply-to", comment_id],
    )
    assert stone.exit_code == 0, stone.output

    default_view = runner.invoke(app, ["comment", "list", "--subject", str(cap), "--json"])
    assert json.loads(default_view.output) == []

    audit_trail = runner.invoke(
        app, ["comment", "list", "--subject", str(cap), "--all", "--json"]
    )
    assert len(json.loads(audit_trail.output)) == 2  # bytes never removed


def test_list_empty_capsule(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(app, ["comment", "list", "--subject", str(cap)])
    assert result.exit_code == 0
    assert "(no comments)" in result.output


def test_list_invalid_file_exits_1(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    (cap / COMMENTS_FILENAME).write_text("{bad json}\n")
    result = runner.invoke(app, ["comment", "list", "--subject", str(cap)])
    assert result.exit_code == 1
    assert "invalid Comment" in result.output


def test_bad_subject_ref_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["comment", "add", "--subject", str(tmp_path / "nope"), "--body", "n"]
    )
    assert result.exit_code == 2


def test_kind_asset_with_a_capsule_subject_is_rejected(tmp_path: Path) -> None:
    """The pairing is enforced in both directions.

    --kind asset over a capsule path is a mismatch: asset comments are
    registry-backed and addressed by an asset:// ref, never by a capsule
    digest. (Was "planned" until ADR-0121 P3 shipped.)
    """
    cap = _make_capsule(tmp_path)
    result = runner.invoke(
        app,
        ["comment", "add", "--subject", str(cap), "--kind", "asset", "--body", "n"],
    )
    assert result.exit_code == 2
    assert "asset://" in result.output


def test_bad_reply_to_ulid_exits_2(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(
        app,
        ["comment", "add", "--subject", str(cap), "--body", "n", "--reply-to", "not-a-ulid"],
    )
    assert result.exit_code == 2
    assert "invalid comment" in result.output


def test_author_falls_back_when_no_local_user(tmp_path: Path) -> None:
    from unittest.mock import patch

    cap = _make_capsule(tmp_path)
    with patch("novafabric.cli.comment.getpass.getuser", side_effect=OSError):
        result = runner.invoke(
            app, ["comment", "add", "--subject", str(cap), "--body", "n", "--json"]
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["author"] == "unknown"


def test_list_all_marks_tombstone_and_redacted(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    added = runner.invoke(
        app,
        ["comment", "add", "--subject", str(cap), "--body", f"k {_SECRET}",
         "--redact", "--json"],
    )
    comment_id = json.loads(added.output)["comment_id"]
    runner.invoke(
        app,
        ["comment", "add", "--subject", str(cap), "--body", "retracting",
         "--tombstone", "--reply-to", comment_id],
    )
    result = runner.invoke(app, ["comment", "list", "--subject", str(cap), "--all"])
    assert result.exit_code == 0
    assert "[redacted]" in result.output
    assert "[tombstone]" in result.output


def test_list_human_output_marks_replies(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    added = runner.invoke(
        app, ["comment", "add", "--subject", str(cap), "--body", "root", "--json"]
    )
    comment_id = json.loads(added.output)["comment_id"]
    runner.invoke(
        app,
        ["comment", "add", "--subject", str(cap), "--body", "confirmed",
         "--reply-to", comment_id],
    )
    result = runner.invoke(app, ["comment", "list", "--subject", str(cap)])
    assert result.exit_code == 0
    assert "root" in result.output
    assert "confirmed" in result.output


# ---------------------------------------------------------------------------
# nova comment thread (ADR-0121 P2 CLI half)
# ---------------------------------------------------------------------------


def _add(cap: Path, body: str, reply_to: str | None = None) -> str:
    """Add a comment via the CLI and return its id."""
    args = ["comment", "add", "--subject", str(cap), "--body", body]
    if reply_to is not None:
        args += ["--reply-to", reply_to]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return read_comments(cap / COMMENTS_FILENAME)[-1].comment_id


def test_thread_returns_chain_root_first(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    root = _add(cap, "root question")
    mid = _add(cap, "first reply", reply_to=root)
    leaf = _add(cap, "second reply", reply_to=mid)

    result = runner.invoke(
        app, ["comment", "thread", leaf, "--subject", str(cap), "--json"]
    )
    assert result.exit_code == 0, result.output
    chain = json.loads(result.output)
    assert [c["comment_id"] for c in chain] == [root, mid, leaf]


def test_thread_indents_by_depth_in_table_output(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    root = _add(cap, "root question")
    leaf = _add(cap, "a reply", reply_to=root)

    result = runner.invoke(app, ["comment", "thread", leaf, "--subject", str(cap)])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines[0].startswith(root)  # root is not indented
    assert lines[1].startswith("  ")  # the reply is


def test_thread_on_a_root_comment_is_just_itself(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    root = _add(cap, "standalone")
    result = runner.invoke(
        app, ["comment", "thread", root, "--subject", str(cap), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert [c["comment_id"] for c in json.loads(result.output)] == [root]


def test_thread_unknown_comment_exits_1(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    _add(cap, "something")
    result = runner.invoke(
        app, ["comment", "thread", "01HXAY7M5JZ8R7K4P9DPBYK2WY", "--subject", str(cap)]
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_thread_orphan_parent_is_tolerated_not_an_error(tmp_path: Path) -> None:
    """A reply whose parent is absent is an orphan root, not a failure.

    The append-only log can legitimately contain this (parent redacted away,
    or written by a tool that dropped it), so the CLI must still resolve.
    """
    cap = _make_capsule(tmp_path)
    orphan = _add(cap, "reply to a ghost", reply_to="01HXAY7M5JZ8R7K4P9DPBYK2WZ")
    result = runner.invoke(
        app, ["comment", "thread", orphan, "--subject", str(cap), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert [c["comment_id"] for c in json.loads(result.output)] == [orphan]


def test_thread_cycle_exits_1_without_a_traceback(tmp_path: Path) -> None:
    """A malformed cycle is corrupt data — report it, never loop forever."""
    cap = _make_capsule(tmp_path)
    a = _add(cap, "first")
    b = _add(cap, "second", reply_to=a)

    # Forge a cycle by rewriting the log: a -> b -> a.
    path = cap / COMMENTS_FILENAME
    records = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    for rec in records:
        if rec.get("comment_id") == a:
            rec["in_reply_to"] = b
    path.write_text("".join(json.dumps(r) + "\n" for r in records))

    result = runner.invoke(app, ["comment", "thread", b, "--subject", str(cap)])
    assert result.exit_code == 1
    assert "cycle" in result.output.lower()
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# asset:// subjects — registry-backed (ADR-0121 P3)
# ---------------------------------------------------------------------------

_ASSET = "asset://model/summarizer@1.2.0"


def _registry(tmp_path: Path, monkeypatch) -> Path:
    """Point the registry at a temp DB and initialise its schema."""
    db = tmp_path / "registry.db"
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(db)
    init_schema(conn)
    conn.close()
    return db


def test_asset_comment_add_and_list(tmp_path: Path, monkeypatch) -> None:
    """asset:// used to exit 2 as 'planned'; it now round-trips."""
    _registry(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["comment", "add", "--subject", _ASSET, "--kind", "asset", "--body", "needs eval"],
    )
    assert result.exit_code == 0, result.output
    assert "registry" in result.output  # reports the backend it wrote to

    listed = runner.invoke(app, ["comment", "list", "--subject", _ASSET, "--json"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert len(rows) == 1
    assert rows[0]["subject"] == _ASSET
    assert rows[0]["body"] == "needs eval"
    assert rows[0]["subject_kind"] == "asset"


def test_asset_comments_are_isolated_per_subject(tmp_path: Path, monkeypatch) -> None:
    _registry(tmp_path, monkeypatch)
    other = "asset://model/other@2.0.0"
    runner.invoke(app, ["comment", "add", "--subject", _ASSET, "--kind", "asset", "--body", "a"])
    runner.invoke(app, ["comment", "add", "--subject", other, "--kind", "asset", "--body", "b"])

    rows = json.loads(
        runner.invoke(app, ["comment", "list", "--subject", _ASSET, "--json"]).output
    )
    assert [r["body"] for r in rows] == ["a"]


def test_asset_comment_threading_works_like_capsules(tmp_path: Path, monkeypatch) -> None:
    """Reader-side semantics are shared, so threads must resolve identically."""
    _registry(tmp_path, monkeypatch)
    runner.invoke(app, ["comment", "add", "--subject", _ASSET, "--kind", "asset", "--body", "root"])
    from novafabric.registry import asset_comments
    from novafabric.registry.store import get_connection

    conn = get_connection(tmp_path / "registry.db")
    root_id = asset_comments.read_comments(conn, _ASSET)[0].comment_id
    conn.close()

    runner.invoke(
        app,
        ["comment", "add", "--subject", _ASSET, "--kind", "asset", "--body", "reply",
         "--reply-to", root_id],
    )
    conn = get_connection(tmp_path / "registry.db")
    leaf_id = asset_comments.read_comments(conn, _ASSET)[-1].comment_id
    conn.close()

    threaded = runner.invoke(
        app, ["comment", "thread", leaf_id, "--subject", _ASSET, "--json"]
    )
    assert threaded.exit_code == 0, threaded.output
    assert [c["comment_id"] for c in json.loads(threaded.output)] == [root_id, leaf_id]


def test_asset_comment_tombstone_hidden_by_default(tmp_path: Path, monkeypatch) -> None:
    """Append-only delete must behave the same in SQLite as in JSONL."""
    _registry(tmp_path, monkeypatch)
    runner.invoke(app, ["comment", "add", "--subject", _ASSET, "--kind", "asset", "--body", "oops"])
    from novafabric.registry import asset_comments
    from novafabric.registry.store import get_connection

    conn = get_connection(tmp_path / "registry.db")
    target = asset_comments.read_comments(conn, _ASSET)[0].comment_id
    conn.close()

    runner.invoke(
        app,
        ["comment", "add", "--subject", _ASSET, "--kind", "asset", "--body", "retract",
         "--tombstone", "--reply-to", target],
    )
    rows = json.loads(
        runner.invoke(app, ["comment", "list", "--subject", _ASSET, "--json"]).output
    )
    assert rows == []  # both the original and the tombstone are hidden


def test_asset_comment_secret_gate_still_applies(tmp_path: Path, monkeypatch) -> None:
    """The ADR-0009 gate must not be bypassed by the new backend."""
    _registry(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["comment", "add", "--subject", _ASSET, "--kind", "asset", "--body", f"key {_SECRET}"],
    )
    assert result.exit_code == 3
