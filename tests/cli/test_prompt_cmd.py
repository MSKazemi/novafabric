"""CLI tests for `nova prompt` (ADR-0112 + ADR-0115, experimental).

Covers:
- `nova prompt --help` smoke (all seven subcommands present)
- register: inline template, text file, chat JSON file, idempotent re-register
- register failures: both/neither template source, bad --config, bad chat file,
  bad slug, variable-mismatch warning
- register (composition): includes-lines printed, unknown ref exit 1
- get: latest, pinned version, --json, frozen ref, not-found exit 1
- list: summaries, one prompt's versions, not-found exit 1
- history: table + --json
- diff: identical exit 0, changed exit 1, json format, bare ref rejected
- compose: manifest table, --json (schema-shaped), --assembled, label
  selector, not-found exit 1
- tree: indented DAG with [label] flag, not-found exit 1
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nova-home"))
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "registry.db"))


def _register(*extra: str) -> None:
    result = runner.invoke(
        app,
        ["prompt", "register", "triage", "--template", "You triage {ticket}.",
         "--var", "ticket", "-m", "first", *extra],
    )
    assert result.exit_code == 0, result.output


def test_prompt_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["prompt", "--help"])
    assert result.exit_code == 0
    for sub in ("register", "get", "list", "history", "diff", "compose", "tree"):
        assert sub in result.output


def test_register_inline_template_creates_v1_with_ref() -> None:
    result = runner.invoke(
        app,
        ["prompt", "register", "triage", "-t", "You triage {ticket}.",
         "--var", "ticket", "-m", "first"],
    )
    assert result.exit_code == 0, result.output
    assert "triage@1" in result.output
    assert "prompt:triage@1+sha256:" in result.output


def test_register_from_text_file(tmp_path: Path) -> None:
    body = tmp_path / "prompt.txt"
    body.write_text("Answer only from {context}.", encoding="utf-8")
    result = runner.invoke(
        app, ["prompt", "register", "ctx", "--file", str(body), "--var", "context"]
    )
    assert result.exit_code == 0, result.output
    assert "ctx@1" in result.output


def test_register_chat_form_from_json_file(tmp_path: Path) -> None:
    messages = tmp_path / "messages.json"
    messages.write_text(
        json.dumps(
            [
                {"role": "system", "content": "You triage tickets."},
                {"role": "user", "content": "Ticket: {ticket_body}"},
            ]
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["prompt", "register", "chat", "--file", str(messages),
         "--var", "ticket_body", "--config", '{"temperature": 0.2}', "--json"],
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["version"] == 1
    assert record["template"][0]["role"] == "system"
    assert record["config"] == {"temperature": 0.2}


def test_register_identical_content_is_idempotent() -> None:
    _register()
    result = runner.invoke(
        app,
        ["prompt", "register", "triage", "-t", "You triage {ticket}.",
         "--var", "ticket", "-m", "same again"],
    )
    assert result.exit_code == 0
    assert "Unchanged content" in result.output
    assert "triage@1" in result.output


def test_register_changed_content_increments() -> None:
    _register()
    result = runner.invoke(
        app, ["prompt", "register", "triage", "-t", "Different body."]
    )
    assert result.exit_code == 0
    assert "triage@2" in result.output


def test_register_requires_exactly_one_template_source(tmp_path: Path) -> None:
    neither = runner.invoke(app, ["prompt", "register", "x"])
    assert neither.exit_code == 2
    f = tmp_path / "t.txt"
    f.write_text("body", encoding="utf-8")
    both = runner.invoke(
        app, ["prompt", "register", "x", "-t", "body", "--file", str(f)]
    )
    assert both.exit_code == 2


def test_register_rejects_bad_config_and_missing_file(tmp_path: Path) -> None:
    bad_cfg = runner.invoke(
        app, ["prompt", "register", "x", "-t", "b", "--config", "not-json"]
    )
    assert bad_cfg.exit_code == 2
    non_object = runner.invoke(
        app, ["prompt", "register", "x", "-t", "b", "--config", "[1]"]
    )
    assert non_object.exit_code == 2
    missing = runner.invoke(
        app, ["prompt", "register", "x", "--file", str(tmp_path / "nope.txt")]
    )
    assert missing.exit_code == 2


def test_register_rejects_bad_chat_json(tmp_path: Path) -> None:
    not_json = tmp_path / "bad.json"
    not_json.write_text("{oops", encoding="utf-8")
    assert (
        runner.invoke(app, ["prompt", "register", "x", "-f", str(not_json)]).exit_code
        == 2
    )
    obj = tmp_path / "obj.json"
    obj.write_text('{"role": "user"}', encoding="utf-8")
    assert (
        runner.invoke(app, ["prompt", "register", "x", "-f", str(obj)]).exit_code == 2
    )
    bad_role = tmp_path / "role.json"
    bad_role.write_text('[{"role": "narrator", "content": "x"}]', encoding="utf-8")
    assert (
        runner.invoke(app, ["prompt", "register", "x", "-f", str(bad_role)]).exit_code
        == 1
    )


def test_register_rejects_non_slug_prompt_id() -> None:
    result = runner.invoke(app, ["prompt", "register", "Not-Valid", "-t", "b"])
    assert result.exit_code == 1


def test_register_warns_on_variable_mismatch() -> None:
    result = runner.invoke(
        app, ["prompt", "register", "warned", "-t", "uses {other}", "--var", "unused"]
    )
    assert result.exit_code == 0
    assert "warning" in result.output


def test_get_latest_and_pinned_version() -> None:
    _register()
    runner.invoke(app, ["prompt", "register", "triage", "-t", "v2 body"])
    latest = runner.invoke(app, ["prompt", "get", "triage"])
    assert latest.exit_code == 0
    assert "triage@2" in latest.output
    pinned = runner.invoke(app, ["prompt", "get", "triage@1", "--json"])
    assert pinned.exit_code == 0
    record = json.loads(pinned.output)
    assert record["version"] == 1
    assert record["template"] == "You triage {ticket}."


def test_get_missing_prompt_exits_1() -> None:
    assert runner.invoke(app, ["prompt", "get", "ghost"]).exit_code == 1
    _register()
    assert runner.invoke(app, ["prompt", "get", "triage@99"]).exit_code == 1
    assert runner.invoke(app, ["prompt", "get", "triage@nope"]).exit_code == 2


def test_list_all_and_single_prompt() -> None:
    empty = runner.invoke(app, ["prompt", "list"])
    assert empty.exit_code == 0
    assert "No managed prompts" in empty.output

    _register()
    runner.invoke(app, ["prompt", "register", "triage", "-t", "v2"])
    all_prompts = runner.invoke(app, ["prompt", "list"])
    assert all_prompts.exit_code == 0
    assert "triage" in all_prompts.output

    versions = runner.invoke(app, ["prompt", "list", "triage"])
    assert versions.exit_code == 0
    assert "Versions of 'triage'" in versions.output

    missing = runner.invoke(app, ["prompt", "list", "ghost"])
    assert missing.exit_code == 1


def test_history_shows_commit_messages() -> None:
    _register()
    runner.invoke(app, ["prompt", "register", "triage", "-t", "v2", "-m", "tighten"])
    result = runner.invoke(app, ["prompt", "history", "triage"])
    assert result.exit_code == 0
    assert "first" in result.output
    assert "tighten" in result.output

    as_json = runner.invoke(app, ["prompt", "history", "triage", "--json"])
    records = json.loads(as_json.output)
    assert [r["version"] for r in records] == [1, 2]

    assert runner.invoke(app, ["prompt", "history", "ghost"]).exit_code == 1


def test_diff_identical_versions_exit_0() -> None:
    _register()
    result = runner.invoke(app, ["prompt", "diff", "triage@1", "triage@1"])
    assert result.exit_code == 0
    assert "No differences" in result.output


def test_diff_changed_versions_exit_1_unified_and_json() -> None:
    _register()
    runner.invoke(
        app,
        ["prompt", "register", "triage", "-t", "New body {ticket}.",
         "--var", "ticket", "--config", '{"temperature": 0.5}'],
    )
    unified = runner.invoke(app, ["prompt", "diff", "triage@1", "triage@2"])
    assert unified.exit_code == 1
    assert "New body" in unified.output

    structured = runner.invoke(
        app, ["prompt", "diff", "triage@1", "triage@2", "--output-format", "json"]
    )
    assert structured.exit_code == 1
    payload = json.loads(structured.output)
    assert payload["identical"] is False
    assert payload["changed"]["template"]["to"] == "New body {ticket}."
    assert payload["added"]["config.temperature"] == 0.5


def test_diff_requires_pinned_versions_and_existing_refs() -> None:
    _register()
    bare = runner.invoke(app, ["prompt", "diff", "triage", "triage@1"])
    assert bare.exit_code == 2
    missing = runner.invoke(app, ["prompt", "diff", "triage@1", "triage@9"])
    assert missing.exit_code == 1


def _register_composed() -> None:
    """footer@1, pre@1 (+label production), root@1 including both."""
    for args in (
        ["prompt", "register", "footer", "-t", "Never guess."],
        ["prompt", "register", "pre", "-t", "You are careful."],
        ["label", "set", "prompt:pre", "production", "1"],
        [
            "prompt", "register", "root",
            "-t", "{{@prompt:pre@production}}\nDo it.\n{{@prompt:footer@1}}",
        ],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output


def test_register_composed_prints_includes_lines() -> None:
    _register_composed()
    result = runner.invoke(
        app, ["prompt", "register", "root2", "-t", "x {{@prompt:footer@1}}"]
    )
    assert result.exit_code == 0, result.output
    assert "includes: @prompt:footer@1" in result.output


def test_register_unknown_composition_ref_exits_1() -> None:
    result = runner.invoke(
        app, ["prompt", "register", "root", "-t", "x {{@prompt:ghost@1}}"]
    )
    assert result.exit_code == 1
    assert "does not resolve" in result.output


def test_compose_table_output_and_selectors() -> None:
    _register_composed()
    result = runner.invoke(app, ["prompt", "compose", "root"])
    assert result.exit_code == 0, result.output
    assert "Composition of root@1" in result.output
    assert "assembled_prompt_hash" in result.output  # rich may wrap the value
    assert "max_depth: 1" in result.output

    pinned = runner.invoke(app, ["prompt", "compose", "root@1"])
    assert pinned.exit_code == 0

    via_label = runner.invoke(app, ["prompt", "compose", "pre@production"])
    assert via_label.exit_code == 0, via_label.output
    assert "Composition of pre@1" in via_label.output


def test_compose_json_is_manifest_shaped() -> None:
    _register_composed()
    result = runner.invoke(app, ["prompt", "compose", "root", "--json"])
    assert result.exit_code == 0, result.output
    manifest = json.loads(result.output)
    assert manifest["schema_version"] == "0.1.0"
    assert manifest["root"]["name"] == "root"
    assert {n["name"] for n in manifest["included"]} == {"root", "pre", "footer"}
    assert len(manifest["edges"]) == 2
    kinds = {e["ref"]: e["selector_kind"] for e in manifest["edges"]}
    assert kinds["@prompt:pre@production"] == "label"
    assert kinds["@prompt:footer@1"] == "version"


def test_compose_assembled_prints_spliced_template() -> None:
    _register_composed()
    result = runner.invoke(app, ["prompt", "compose", "root", "--assembled"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "You are careful.\nDo it.\nNever guess."


def test_compose_missing_prompt_exits_1() -> None:
    assert runner.invoke(app, ["prompt", "compose", "ghost"]).exit_code == 1
    _register_composed()
    assert runner.invoke(app, ["prompt", "compose", "root@nolabel"]).exit_code == 1


def test_tree_shows_children_and_label_flag() -> None:
    _register_composed()
    result = runner.invoke(app, ["prompt", "tree", "root"])
    assert result.exit_code == 0, result.output
    assert "root@1" in result.output
    assert "pre" in result.output
    assert "footer" in result.output
    assert "@production → v1" in result.output
    assert "[label]" in result.output
    assert "@1 → v1" in result.output


def test_tree_missing_prompt_exits_1() -> None:
    assert runner.invoke(app, ["prompt", "tree", "ghost"]).exit_code == 1
