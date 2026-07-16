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


def test_asset_subject_is_planned_not_implemented(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["comment", "add", "--subject", "asset://prompt/triage@1.0.0", "--body", "n"],
    )
    assert result.exit_code == 2
    assert "planned" in result.output


def test_tombstone_requires_reply_to(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(
        app, ["comment", "add", "--subject", str(cap), "--body", "n", "--tombstone"]
    )
    assert result.exit_code == 2
    assert "--reply-to" in result.output


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


def test_kind_asset_is_planned_not_implemented(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(
        app,
        ["comment", "add", "--subject", str(cap), "--kind", "asset", "--body", "n"],
    )
    assert result.exit_code == 2
    assert "planned" in result.output


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
