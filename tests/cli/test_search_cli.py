"""CLI tests for `nova search` (ADR-0204 P1, experimental).

Covers: --help smoke, --reindex backfill + idempotency, a real search over
a fixture capsule (human + --json output), operator-injection queries, the
no-argument usage error, and clean degradation without FTS5.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.query.content_index import fts5_available

runner = CliRunner()

requires_fts5 = pytest.mark.skipif(
    not fts5_available(), reason="sqlite build lacks FTS5"
)


def _make_capsule(base: Path, run_id: str) -> Path:
    cap = base / run_id
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text(yaml.dump({
        "run_id": run_id,
        "status": "success",
        "created_at": "2026-07-24T10:00:00Z",
        "command": ["python", "agent.py"],
    }))
    (cap / "model-calls.jsonl").write_text(json.dumps({
        "gen_ai.request.messages": [
            {"role": "user", "content": "which run mentioned invoice INV-2291?"},
        ],
    }) + "\n")
    (cap / "tool-calls.jsonl").write_text(json.dumps({
        "tool_name": "shell", "arguments": {"cmd": "rm -rf /tmp/scratch"},
    }) + "\n")
    return cap


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    base = tmp_path / "capsules"
    base.mkdir()
    _make_capsule(base, "run-aaa")
    return {
        "capsules": str(base),
        "db": str(tmp_path / "registry.db"),
    }


def _search(*args: str) -> object:
    return runner.invoke(app, ["search", *args])


def test_help_smoke() -> None:
    result = runner.invoke(app, ["search", "--help"])
    assert result.exit_code == 0
    assert "experimental" in result.output
    assert_flag_in_help(result, "--reindex")


def test_no_text_and_no_reindex_is_usage_error(tmp_path: Path) -> None:
    result = _search("--db-path", str(tmp_path / "r.db"))
    assert result.exit_code == 2
    assert "provide search text" in result.output


@requires_fts5
def test_reindex_then_search_round_trip(env: dict[str, str]) -> None:
    result = _search(
        "--reindex", "--capsule-dir", env["capsules"], "--db-path", env["db"]
    )
    assert result.exit_code == 0
    assert "Reindexed 1 capsule(s)" in result.output

    result = _search("invoice", "--db-path", env["db"])
    assert result.exit_code == 0
    assert "run-aaa" in result.output
    assert "«invoice»" in result.output
    assert "model-call-messages:model-calls.jsonl:1" in result.output

    # Reindex again — idempotent, nothing new to do.
    result = _search(
        "--reindex", "--capsule-dir", env["capsules"], "--db-path", env["db"]
    )
    assert result.exit_code == 0
    assert "Reindexed 0 capsule(s)" in result.output


@requires_fts5
def test_json_output_shape(env: dict[str, str]) -> None:
    _search("--reindex", "--capsule-dir", env["capsules"], "--db-path", env["db"])
    result = _search("invoice", "--json", "--db-path", env["db"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["snippet_markers"] == ["«", "»"]
    (item,) = payload["items"]
    assert item["run_id"] == "run-aaa"
    assert item["matches_truncated"] is False
    (match,) = item["matches"]
    assert match["stream"] == "model-call-messages"
    assert match["ref"] == "model-calls.jsonl"
    assert match["line_no"] == 1
    assert "«invoice»" in match["snippet"]


@requires_fts5
@pytest.mark.parametrize(
    "query", ["rm -rf", 'say "hello"', "a OR b", "NEAR(x)", "text:secret"]
)
def test_operator_injection_queries_do_not_error(
    env: dict[str, str], query: str
) -> None:
    _search("--reindex", "--capsule-dir", env["capsules"], "--db-path", env["db"])
    result = _search(query, "--db-path", env["db"])
    assert result.exit_code == 0
    if query == "rm -rf":  # literal match, not exclusion
        assert "run-aaa" in result.output


@requires_fts5
def test_no_matches_message(env: dict[str, str]) -> None:
    _search("--reindex", "--capsule-dir", env["capsules"], "--db-path", env["db"])
    result = _search("zzz-not-there", "--db-path", env["db"])
    assert result.exit_code == 0
    assert "No matches." in result.output


@requires_fts5
def test_stream_filter(env: dict[str, str]) -> None:
    _search("--reindex", "--capsule-dir", env["capsules"], "--db-path", env["db"])
    result = _search(
        "scratch", "--stream", "tool-call-arguments", "--db-path", env["db"]
    )
    assert result.exit_code == 0
    assert "run-aaa" in result.output
    result = _search(
        "scratch", "--stream", "trace", "--db-path", env["db"]
    )
    assert result.exit_code == 0
    assert "No matches." in result.output
    result = _search(
        "scratch", "--stream", "not-a-stream", "--db-path", env["db"]
    )
    assert result.exit_code == 1


def test_fts5_unavailable_message(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.query import content_index as ci

    monkeypatch.setattr(ci, "fts5_available", lambda: False)
    result = _search("anything", "--db-path", env["db"])
    assert result.exit_code == 1
    assert "requires SQLite FTS5" in result.output

    result = _search(
        "--reindex", "--capsule-dir", env["capsules"], "--db-path", env["db"]
    )
    assert result.exit_code == 1
    assert "requires SQLite FTS5" in result.output
