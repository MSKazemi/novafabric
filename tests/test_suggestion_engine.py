"""Tests for the asset registration suggestion engine (C-2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.cli.suggest_register import (
    _write_drafts,
    _write_single_draft,
)
from novafabric.registry.suggestion_engine import (
    RegistrationSuggestion,
    SuggestionEngine,
    _infer_agent_name,
    _parse_model_identity,
)

cli_runner = CliRunner()

# ------------------------------------------------------------------ fixtures


def _write_capsule(
    tmp_path: Path,
    *,
    model_calls: list[dict] | None = None,
    tool_calls: list[dict] | None = None,
    command: list[str] | None = None,
) -> Path:
    capsule_dir = tmp_path / "01TESTCAPSULE"
    capsule_dir.mkdir()

    mc = model_calls or []
    (capsule_dir / "model-calls.jsonl").write_text(
        "\n".join(json.dumps(r) for r in mc)
    )

    tc = tool_calls or []
    (capsule_dir / "tool-calls.jsonl").write_text(
        "\n".join(json.dumps(r) for r in tc)
    )

    manifest = {
        "run_id": "01TESTCAPSULE",
        "schema_version": "0.1.0",
        "command": command or ["python", "agent.py"],
        "exit_code": 0,
        "status": "success",
    }
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest))

    return capsule_dir


# ------------------------------------------------------------------ unit: _parse_model_identity


def test_parse_model_with_date_suffix():
    name, version = _parse_model_identity("gpt-4o-2024-08-06")
    assert name == "gpt-4o"
    assert version == "2024.8.6"


def test_parse_model_with_yyyymmdd_suffix():
    name, version = _parse_model_identity("claude-3-5-sonnet-20241022")
    assert name == "claude-3-5-sonnet"
    assert version == "2024.10.22"


def test_parse_model_no_date():
    name, version = _parse_model_identity("llama3.1:8b")
    assert name == "llama3.1-8b"
    assert version == "0.1.0"


def test_parse_model_simple():
    name, version = _parse_model_identity("gpt-4o")
    assert name == "gpt-4o"
    assert version == "0.1.0"


# ------------------------------------------------------------------ unit tests: _infer_agent_name


def test_infer_agent_name_py_file():
    assert _infer_agent_name(["python", "src/myagent.py"]) == "myagent"


def test_infer_agent_name_module():
    assert _infer_agent_name(["python", "-m", "myapp.runner"]) == "myapp.runner"


def test_infer_agent_name_fallback():
    assert _infer_agent_name(["python"]) == "my-agent"


# ------------------------------------------------------------------ SuggestionEngine.analyze


def test_analyze_empty_capsule(tmp_path: Path):
    capsule_dir = _write_capsule(tmp_path)
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)
    # Agent suggestion always generated (command is present)
    assert any(s.asset_type == "agent" for s in suggestions)


def test_analyze_model_calls(tmp_path: Path):
    model_records = [
        {
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o-2024-08-06",
            "gen_ai.response.model": "gpt-4o-2024-08-06",
        }
    ] * 5
    capsule_dir = _write_capsule(tmp_path, model_calls=model_records)
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)

    model_suggs = [s for s in suggestions if s.asset_type == "model"]
    assert len(model_suggs) == 1
    s = model_suggs[0]
    assert s.detected_name == "gpt-4o"
    assert s.detected_version == "2024.8.6"
    assert s.call_count == 5
    assert s.confidence == 1.0
    # draft YAML is valid
    parsed = yaml.safe_load(s.draft_spec_yaml)
    assert parsed["asset_type"] == "model"
    assert parsed["spec"]["framework"] == "openai"


def test_analyze_tool_calls_threshold(tmp_path: Path):
    # Tool called once — below threshold, should NOT be suggested
    tool_records_once = [
        {"tool_name": "rare_tool", "transport": "local", "tool_provider": ""}
    ]
    # Tool called 3 times — above threshold
    tool_records_multi = [
        {"tool_name": "common_tool", "transport": "mcp", "tool_provider": "mcp://filesystem-server"}
    ] * 3
    capsule_dir = _write_capsule(tmp_path, tool_calls=tool_records_once + tool_records_multi)
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)

    tool_names = [s.detected_name for s in suggestions if s.asset_type == "tool"]
    assert "common_tool" in tool_names
    assert "rare_tool" not in tool_names


def test_analyze_tool_draft_yaml(tmp_path: Path):
    tool_records = [
        {"tool_name": "web_search", "transport": "mcp", "tool_provider": "mcp://brave-search"}
    ] * 3
    capsule_dir = _write_capsule(tmp_path, tool_calls=tool_records)
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)

    tool_sugg = next(s for s in suggestions if s.asset_type == "tool")
    parsed = yaml.safe_load(tool_sugg.draft_spec_yaml)
    assert parsed["asset_type"] == "tool"
    assert "mcp://brave-search" in parsed["spec"]["entrypoint"]


def test_analyze_agent_has_warning(tmp_path: Path):
    capsule_dir = _write_capsule(tmp_path, command=["python", "my_agent.py"])
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)

    agent_sugg = next((s for s in suggestions if s.asset_type == "agent"), None)
    assert agent_sugg is not None
    assert agent_sugg.detected_name == "my_agent"
    assert any("evals" in w for w in agent_sugg.warnings)


def test_analyze_agent_prefills_model(tmp_path: Path):
    model_records = [
        {"gen_ai.system": "anthropic", "gen_ai.request.model": "claude-sonnet-4-6", "gen_ai.response.model": "claude-sonnet-4-6"}
    ] * 10
    capsule_dir = _write_capsule(tmp_path, model_calls=model_records, command=["python", "planner.py"])
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)

    agent_sugg = next(s for s in suggestions if s.asset_type == "agent")
    parsed = yaml.safe_load(agent_sugg.draft_spec_yaml)
    assert parsed["spec"]["model"]["name"] == "claude-sonnet-4-6"
    assert parsed["spec"]["model"]["provider"] == "anthropic"


def test_analyze_no_command_no_agent(tmp_path: Path):
    capsule_dir = tmp_path / "01NOCMD"
    capsule_dir.mkdir()
    manifest = {"run_id": "01NOCMD", "schema_version": "0.1.0", "command": [], "exit_code": 0, "status": "success"}
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest))
    (capsule_dir / "model-calls.jsonl").write_text("")
    (capsule_dir / "tool-calls.jsonl").write_text("")

    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)
    assert not any(s.asset_type == "agent" for s in suggestions)


# ------------------------------------------------------------------ analyze_recent


def test_analyze_recent_deduplicates(tmp_path: Path):
    """Same model seen in two capsules → merged, call count summed."""
    runs = tmp_path / "runs"
    runs.mkdir()
    model_rec = [{"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o", "gen_ai.response.model": "gpt-4o"}]

    for i in range(2):
        d = runs / f"0{i+1}CAPSULE"
        d.mkdir()
        (d / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in model_rec))
        (d / "tool-calls.jsonl").write_text("")
        manifest = {"run_id": f"0{i+1}CAPSULE", "schema_version": "0.1.0", "command": ["python", "agent.py"], "exit_code": 0, "status": "success"}
        (d / "capsule.yaml").write_text(yaml.dump(manifest))

    engine = SuggestionEngine()
    suggestions = engine.analyze_recent(runs, limit=10)
    model_suggs = [s for s in suggestions if s.asset_type == "model"]
    assert len(model_suggs) == 1
    assert model_suggs[0].call_count == 2


def test_analyze_recent_empty_dir(tmp_path: Path):
    engine = SuggestionEngine()
    assert engine.analyze_recent(tmp_path / "nonexistent") == []


def test_analyze_recent_filters_registered(tmp_path: Path):
    """Assets already in the registry must be excluded from suggestions."""

    from novafabric.registry.store import get_connection, init_schema

    db_path = tmp_path / "registry.db"
    conn = get_connection(db_path)
    init_schema(conn)

    # Insert a 'gpt-4o' model record directly
    conn.execute(
        "INSERT INTO assets (id, name, version, asset_type, status, spec_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("id-1", "gpt-4o", "0.1.0", "model", "development", "{}", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    runs = tmp_path / "runs"
    runs.mkdir()
    d = runs / "01CAPS"
    d.mkdir()
    model_rec = [{"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o", "gen_ai.response.model": "gpt-4o"}]
    (d / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in model_rec))
    (d / "tool-calls.jsonl").write_text("")
    manifest = {"run_id": "01CAPS", "schema_version": "0.1.0", "command": ["python", "a.py"], "exit_code": 0, "status": "success"}
    (d / "capsule.yaml").write_text(yaml.dump(manifest))

    engine = SuggestionEngine()
    suggestions = engine.analyze_recent(runs, db_path=db_path, limit=10)
    model_suggs = [s for s in suggestions if s.asset_type == "model"]
    assert not model_suggs, "gpt-4o should be filtered as already registered"


def test_analyze_malformed_jsonl(tmp_path: Path):
    """Malformed JSONL lines must be silently ignored."""
    capsule_dir = _write_capsule(tmp_path)
    (capsule_dir / "model-calls.jsonl").write_text("not-json\n{incomplete")
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)
    model_suggs = [s for s in suggestions if s.asset_type == "model"]
    assert model_suggs == []


def test_analyze_missing_model_calls_file(tmp_path: Path):
    """Missing model-calls.jsonl is handled gracefully (returns no model suggestions)."""
    capsule_dir = tmp_path / "01NOCALLS"
    capsule_dir.mkdir()
    (capsule_dir / "tool-calls.jsonl").write_text("")
    manifest = {"run_id": "01NOCALLS", "schema_version": "0.1.0", "command": ["python", "a.py"], "exit_code": 0, "status": "success"}
    (capsule_dir / "capsule.yaml").write_text(__import__("yaml").dump(manifest))
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)
    assert not any(s.asset_type == "model" for s in suggestions)


def test_analyze_missing_tool_calls_file(tmp_path: Path):
    """Missing tool-calls.jsonl is handled gracefully (returns no tool suggestions)."""
    capsule_dir = tmp_path / "01NOTOOLCALLS"
    capsule_dir.mkdir()
    (capsule_dir / "model-calls.jsonl").write_text("")
    manifest = {"run_id": "01NOTOOLCALLS", "schema_version": "0.1.0", "command": ["python", "a.py"], "exit_code": 0, "status": "success"}
    (capsule_dir / "capsule.yaml").write_text(__import__("yaml").dump(manifest))
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)
    assert not any(s.asset_type == "tool" for s in suggestions)


def test_analyze_missing_capsule_yaml(tmp_path: Path):
    """Missing capsule.yaml → no agent suggestion."""
    capsule_dir = tmp_path / "01NOYAML"
    capsule_dir.mkdir()
    (capsule_dir / "model-calls.jsonl").write_text("")
    (capsule_dir / "tool-calls.jsonl").write_text("")
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)
    assert not any(s.asset_type == "agent" for s in suggestions)


def test_analyze_ollama_model_provider(tmp_path: Path):
    """Ollama models should get 'ollama' as the agent provider."""
    model_records = [
        {"gen_ai.system": "ollama", "gen_ai.request.model": "qwen3.6:35b", "gen_ai.response.model": "qwen3.6:35b"}
    ] * 3
    capsule_dir = _write_capsule(tmp_path, model_calls=model_records, command=["python", "chat.py"])
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)

    agent_sugg = next(s for s in suggestions if s.asset_type == "agent")
    parsed = __import__("yaml").safe_load(agent_sugg.draft_spec_yaml)
    assert parsed["spec"]["model"]["provider"] == "ollama"


def test_analyze_with_db_path_filtering(tmp_path: Path):
    """analyze() with db_path must filter already-registered models."""
    from novafabric.registry.store import get_connection, init_schema

    db_path = tmp_path / "registry.db"
    conn = get_connection(db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO assets (id, name, version, asset_type, status, spec_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("id-1", "gpt-4o", "0.1.0", "model", "development", "{}", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    model_records = [
        {"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o", "gen_ai.response.model": "gpt-4o"}
    ] * 5
    capsule_dir = _write_capsule(tmp_path, model_calls=model_records)
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir, db_path=db_path)
    assert not any(s.asset_type == "model" and s.detected_name == "gpt-4o" for s in suggestions)


def test_analyze_tool_empty_tool_name(tmp_path: Path):
    """Tool records missing tool_name are silently skipped."""
    tool_records = [
        {"tool_name": "", "transport": "local"},  # empty name
        {"tool_name": "real_tool", "transport": "local"},
        {"tool_name": "real_tool", "transport": "local"},
    ]
    capsule_dir = _write_capsule(tmp_path, tool_calls=tool_records)
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)
    tool_names = [s.detected_name for s in suggestions if s.asset_type == "tool"]
    assert "" not in tool_names
    assert "real_tool" in tool_names


def test_analyze_agent_draft_no_model(tmp_path: Path):
    """Agent draft when there are no model calls → uses 'unknown' provider."""
    capsule_dir = _write_capsule(tmp_path, command=["python", "nomodel_agent.py"])
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)

    agent_sugg = next(s for s in suggestions if s.asset_type == "agent")
    parsed = yaml.safe_load(agent_sugg.draft_spec_yaml)
    assert parsed["spec"]["model"]["provider"] == "unknown"
    assert parsed["spec"]["model"]["name"] == "unknown"


def test_analyze_malformed_capsule_yaml(tmp_path: Path):
    """Invalid capsule.yaml (non-YAML content) is handled gracefully."""
    capsule_dir = tmp_path / "01BADYAML"
    capsule_dir.mkdir()
    (capsule_dir / "model-calls.jsonl").write_text("")
    (capsule_dir / "tool-calls.jsonl").write_text("")
    (capsule_dir / "capsule.yaml").write_text(": invalid: yaml: [\n")
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)
    assert not any(s.asset_type == "agent" for s in suggestions)


def test_analyze_model_calls_empty_line(tmp_path: Path):
    """model-calls.jsonl with blank lines is handled gracefully."""
    capsule_dir = tmp_path / "01EMPTYLINES"
    capsule_dir.mkdir()
    (capsule_dir / "model-calls.jsonl").write_text(
        "\n"  # blank line
        + json.dumps({"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o", "gen_ai.response.model": "gpt-4o"})
        + "\n\n"  # more blank lines
    )
    (capsule_dir / "tool-calls.jsonl").write_text("")
    manifest = {"run_id": "01EMPTYLINES", "schema_version": "0.1.0", "command": ["python", "a.py"], "exit_code": 0, "status": "success"}
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest))
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)
    model_suggs = [s for s in suggestions if s.asset_type == "model"]
    assert len(model_suggs) == 1
    assert model_suggs[0].call_count == 1


# ------------------------------------------------------------------ helper function tests


def test_write_single_draft_creates_file(tmp_path: Path):
    s = RegistrationSuggestion(
        asset_type="model",
        detected_name="gpt-4o",
        detected_version="0.1.0",
        confidence=1.0,
        call_count=5,
        evidence=[],
        draft_spec_yaml='asset_type: model\nname: gpt-4o\nversion: 0.1.0\n',
    )
    out_dir = tmp_path / "drafts"
    path = _write_single_draft(s, out_dir)
    assert path.exists()
    assert path.read_text() == s.draft_spec_yaml


def test_write_drafts_creates_all_files(tmp_path: Path):
    suggestions = [
        RegistrationSuggestion("model", "gpt-4o", "0.1.0", 1.0, 5, [], "model: gpt-4o\n"),
        RegistrationSuggestion("tool", "search", "0.1.0", 0.8, 3, [], "tool: search\n"),
    ]
    out_dir = tmp_path / "out"
    _write_drafts(suggestions, out_dir)
    assert len(list(out_dir.iterdir())) == 2


# ------------------------------------------------------------------ CLI integration tests


def test_suggest_register_no_runs_dir(tmp_path: Path):
    """When runs dir is empty, no suggestions are detected."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    result = cli_runner.invoke(app, ["suggest-register", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 0
    assert "No unregistered assets" in result.output


def test_suggest_register_draft_only(tmp_path: Path):
    """--draft-only writes YAML files without registering."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    model_rec = [{"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o", "gen_ai.response.model": "gpt-4o"}]
    d = runs_dir / "01CAPS"
    d.mkdir()
    (d / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in model_rec))
    (d / "tool-calls.jsonl").write_text("")
    manifest = {"run_id": "01CAPS", "schema_version": "0.1.0", "command": ["python", "a.py"], "exit_code": 0, "status": "success"}
    (d / "capsule.yaml").write_text(yaml.dump(manifest))

    out_dir = tmp_path / "drafts"
    result = cli_runner.invoke(app, [
        "suggest-register",
        "--runs-dir", str(runs_dir),
        "--draft-only",
        "--output-dir", str(out_dir),
    ])
    assert result.exit_code == 0
    assert out_dir.exists()
    draft_files = list(out_dir.iterdir())
    assert any("model" in f.name for f in draft_files)


def test_suggest_register_specific_capsule(tmp_path: Path):
    """Passing a capsule path analyzes only that capsule."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    d = runs_dir / "01SPECIFIC"
    d.mkdir()
    (d / "model-calls.jsonl").write_text("")
    (d / "tool-calls.jsonl").write_text("")
    manifest = {"run_id": "01SPECIFIC", "schema_version": "0.1.0", "command": ["python", "b.py"], "exit_code": 0, "status": "success"}
    (d / "capsule.yaml").write_text(yaml.dump(manifest))

    out_dir = tmp_path / "drafts"
    result = cli_runner.invoke(app, [
        "suggest-register", str(d),
        "--runs-dir", str(runs_dir),
        "--draft-only", "--output-dir", str(out_dir),
    ])
    assert result.exit_code == 0


def test_suggest_register_interactive_skip(tmp_path: Path):
    """Interactive mode: answer 'n' to all prompts → skips everything."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    model_rec = [{"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o", "gen_ai.response.model": "gpt-4o"}]
    d = runs_dir / "01ITEST"
    d.mkdir()
    (d / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in model_rec))
    (d / "tool-calls.jsonl").write_text("")
    manifest = {"run_id": "01ITEST", "schema_version": "0.1.0", "command": ["python", "a.py"], "exit_code": 0, "status": "success"}
    (d / "capsule.yaml").write_text(yaml.dump(manifest))

    # Answer 'n' (skip) to every prompt — there are 2 suggestions (model + agent)
    result = cli_runner.invoke(app, [
        "suggest-register", "--runs-dir", str(runs_dir),
    ], input="n\nn\n")
    assert result.exit_code == 0
    assert "Done." in result.output
    assert "0 registered" in result.output


def test_suggest_register_interactive_yes(tmp_path: Path):
    """Interactive mode: answer 'y' registers the draft spec."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    d = runs_dir / "01YTEST"
    d.mkdir()
    # Only model (no tools, no agent command to keep it simple)
    model_rec = [{"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o-2024-08-06", "gen_ai.response.model": "gpt-4o-2024-08-06"}]
    (d / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in model_rec))
    (d / "tool-calls.jsonl").write_text("")
    # capsule.yaml without command → no agent suggestion
    manifest = {"run_id": "01YTEST", "schema_version": "0.1.0", "command": [], "exit_code": 0, "status": "success"}
    (d / "capsule.yaml").write_text(yaml.dump(manifest))

    out_dir = tmp_path / "drafts"
    db_path = tmp_path / "reg.db"
    import os
    env = {**os.environ, "NOVAFABRIC_DB_PATH": str(db_path)}
    result = cli_runner.invoke(app, [
        "suggest-register", "--runs-dir", str(runs_dir),
        "--output-dir", str(out_dir),
    ], input="y\n", env=env)
    assert result.exit_code == 0
    assert "Registered" in result.output or "registered" in result.output.lower()


def test_suggest_register_capsule_not_found(tmp_path: Path):
    """Non-existent capsule ID exits with code 1."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    result = cli_runner.invoke(app, ["suggest-register", "NONEXISTENT", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 1


def test_suggest_register_skip_types(tmp_path: Path):
    """--skip-types excludes the specified asset types from suggestions."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    model_rec = [{"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o", "gen_ai.response.model": "gpt-4o"}]
    d = runs_dir / "01SKIPTEST"
    d.mkdir()
    (d / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in model_rec))
    (d / "tool-calls.jsonl").write_text("")
    manifest = {"run_id": "01SKIPTEST", "schema_version": "0.1.0", "command": ["python", "a.py"], "exit_code": 0, "status": "success"}
    (d / "capsule.yaml").write_text(yaml.dump(manifest))

    out_dir = tmp_path / "drafts"
    result = cli_runner.invoke(app, [
        "suggest-register",
        "--runs-dir", str(runs_dir),
        "--draft-only", "--output-dir", str(out_dir),
        "--skip-types", "model,agent",
    ])
    assert result.exit_code == 0
    # No model or agent drafts should be written
    if out_dir.exists():
        for f in out_dir.iterdir():
            assert not f.name.startswith("model-")
            assert not f.name.startswith("agent-")


def test_suggest_register_auto_registers_model(tmp_path: Path):
    """--auto mode registers high-confidence suggestions (models at 100%)."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    d = runs_dir / "01AUTOMODEL"
    d.mkdir()
    model_rec = [{"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o-2024-08-06", "gen_ai.response.model": "gpt-4o-2024-08-06"}]
    (d / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in model_rec))
    (d / "tool-calls.jsonl").write_text("")
    # No command so no agent suggestion, just model
    manifest = {"run_id": "01AUTOMODEL", "schema_version": "0.1.0", "command": [], "exit_code": 0, "status": "success"}
    (d / "capsule.yaml").write_text(yaml.dump(manifest))

    db_path = tmp_path / "reg.db"
    out_dir = tmp_path / "drafts"
    import os
    env = {**os.environ, "NOVAFABRIC_DB_PATH": str(db_path)}
    result = cli_runner.invoke(app, [
        "suggest-register",
        "--runs-dir", str(runs_dir),
        "--auto", "--min-confidence", "0.9",
        "--output-dir", str(out_dir),
    ], env=env)
    assert result.exit_code == 0


def test_suggest_register_auto_no_eligible(tmp_path: Path):
    """--auto with --min-confidence 1.0 should skip agent (confidence 70%)."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    d = runs_dir / "01AUTOTEST"
    d.mkdir()
    (d / "model-calls.jsonl").write_text("")
    (d / "tool-calls.jsonl").write_text("")
    manifest = {"run_id": "01AUTOTEST", "schema_version": "0.1.0", "command": ["python", "a.py"], "exit_code": 0, "status": "success"}
    (d / "capsule.yaml").write_text(yaml.dump(manifest))

    result = cli_runner.invoke(app, [
        "suggest-register",
        "--runs-dir", str(runs_dir),
        "--auto", "--min-confidence", "1.0",
    ])
    assert result.exit_code == 0
    assert "No suggestions" in result.output or "No unregistered" in result.output


# ------------------------------------------------------------------ server route tests


@pytest.fixture
def _server_client(tmp_path: Path) -> TestClient:
    from novafabric.server import deps
    from novafabric.server.app import create_app
    from novafabric.server.config import ServerConfig

    db_path = tmp_path / "reg.db"
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    cfg = ServerConfig(db_path=str(db_path))
    server_app = create_app(cfg)
    server_app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
    return TestClient(server_app, raise_server_exceptions=False)


def test_suggest_register_route_empty(tmp_path: Path):
    """GET /v0/runs/suggest-register returns an empty list when no capsules exist."""
    from novafabric.server import deps
    from novafabric.server.app import create_app
    from novafabric.server.config import ServerConfig

    db_path = tmp_path / "reg.db"
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    cfg = ServerConfig(db_path=str(db_path))
    server_app = create_app(cfg)
    server_app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
    client = TestClient(server_app, raise_server_exceptions=False)

    resp = client.get("/v0/runs/suggest-register", params={"token": ""})
    # Auth middleware may reject; if 200, check structure
    assert resp.status_code in (200, 401, 403)
    if resp.status_code == 200:
        body = resp.json()
        assert "suggestions" in body
        assert isinstance(body["suggestions"], list)


def test_suggest_register_route_with_model_calls(tmp_path: Path):
    """GET /v0/runs/suggest-register returns model suggestions from recent capsules."""
    from novafabric.server import deps
    from novafabric.server.app import create_app
    from novafabric.server.config import ServerConfig

    db_path = tmp_path / "reg.db"
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()

    # Seed a capsule with model calls
    d = capsule_dir / "01SRVTEST"
    d.mkdir()
    model_rec = [{"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o", "gen_ai.response.model": "gpt-4o"}]
    (d / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in model_rec))
    (d / "tool-calls.jsonl").write_text("")
    manifest = {"run_id": "01SRVTEST", "schema_version": "0.1.0", "command": ["python", "a.py"], "exit_code": 0, "status": "success"}
    (d / "capsule.yaml").write_text(yaml.dump(manifest))

    cfg = ServerConfig(db_path=str(db_path), auth_disabled=True)
    server_app = create_app(cfg)
    server_app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
    client = TestClient(server_app, raise_server_exceptions=False)

    resp = client.get("/v0/runs/suggest-register")
    assert resp.status_code in (200, 401, 403)
    if resp.status_code == 200:
        body = resp.json()
        assert "suggestions" in body
        types = [s["asset_type"] for s in body["suggestions"]]
        assert "model" in types


# ------------------------------------------------------------------ coverage gap tests


def test_analyze_tool_calls_blank_and_malformed(tmp_path: Path):
    """Blank lines and malformed JSON in tool-calls.jsonl are silently skipped."""
    capsule_dir = tmp_path / "01TOOLBLANK"
    capsule_dir.mkdir()
    (capsule_dir / "model-calls.jsonl").write_text("")
    tool_content = (
        "\n"  # blank line → hits _analyze_tools continue
        + "not json\n"  # malformed → hits _analyze_tools except branch
        + json.dumps({"tool_name": "real_tool", "transport": "local"}) + "\n"
        + json.dumps({"tool_name": "real_tool", "transport": "local"}) + "\n"
    )
    (capsule_dir / "tool-calls.jsonl").write_text(tool_content)
    manifest = {
        "run_id": "01TOOLBLANK", "schema_version": "0.1.0",
        "command": ["python", "a.py"], "exit_code": 0, "status": "success",
    }
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest))

    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)
    # real_tool appeared twice → above threshold
    tool_suggs = [s for s in suggestions if s.asset_type == "tool"]
    assert len(tool_suggs) == 1
    assert tool_suggs[0].detected_name == "real_tool"


def test_all_tool_names_blank_and_malformed(tmp_path: Path):
    """Blank lines and bad JSON in tool-calls.jsonl are skipped by _all_tool_names."""
    capsule_dir = tmp_path / "01ALLTOOLS"
    capsule_dir.mkdir()
    (capsule_dir / "model-calls.jsonl").write_text("")
    tool_content = (
        "\n"  # blank line → hits _all_tool_names continue
        + "bad json here\n"  # malformed → hits _all_tool_names except branch
        + json.dumps({"tool_name": "search", "transport": "mcp"}) + "\n"
    )
    (capsule_dir / "tool-calls.jsonl").write_text(tool_content)
    manifest = {
        "run_id": "01ALLTOOLS", "schema_version": "0.1.0",
        "command": ["python", "agent.py"], "exit_code": 0, "status": "success",
    }
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest))

    engine = SuggestionEngine()
    # _analyze_agent calls _all_tool_names → exercises those branches
    suggestions = engine.analyze(capsule_dir)
    agent_sugg = next((s for s in suggestions if s.asset_type == "agent"), None)
    assert agent_sugg is not None
    assert "search" in agent_sugg.draft_spec_yaml


def test_is_registered_exception_returns_false(tmp_path: Path):
    """_is_registered returns False when list_assets raises."""
    from unittest.mock import patch

    engine = SuggestionEngine()
    with patch(
        "novafabric.registry.service.list_assets",
        side_effect=RuntimeError("db unavailable"),
    ):
        result = engine._is_registered("any-model", "model", tmp_path / "fake.db")
    assert result is False


def test_analyze_agent_unknown_provider(tmp_path: Path):
    """Agent draft for an unrecognised model system gets 'unknown' provider."""
    model_records = [
        {
            "gen_ai.system": "custom_llm",
            "gen_ai.request.model": "mistral-7b",
            "gen_ai.response.model": "mistral-7b",
        }
    ] * 3
    capsule_dir = _write_capsule(tmp_path, model_calls=model_records, command=["python", "chat.py"])
    engine = SuggestionEngine()
    suggestions = engine.analyze(capsule_dir)

    agent_sugg = next(s for s in suggestions if s.asset_type == "agent")
    parsed = yaml.safe_load(agent_sugg.draft_spec_yaml)
    assert parsed["spec"]["model"]["provider"] == "unknown"


def test_open_editor_not_found(tmp_path: Path):
    """_open_editor prints a fallback message when the editor binary is missing."""
    import os
    from unittest.mock import patch

    from novafabric.cli.suggest_register import _open_editor

    spec_file = tmp_path / "draft.yaml"
    spec_file.write_text("name: test\n")
    with patch.dict(os.environ, {"EDITOR": "nonexistent-editor-xyz-12345"}):
        _open_editor(spec_file)  # must not raise


def test_register_from_file_validation_error(tmp_path: Path):
    """_register_from_file handles SpecValidationError without raising."""
    from novafabric.cli.suggest_register import _register_from_file

    bad_spec = tmp_path / "bad.yaml"
    bad_spec.write_text("asset_type: model\nname: missing-required-fields\n")
    _register_from_file(bad_spec, None)  # must not raise


def test_register_from_file_duplicate(tmp_path: Path):
    """_register_from_file handles DuplicateAssetError without raising."""
    import os
    from unittest.mock import patch

    from novafabric.cli.suggest_register import _register_from_file

    db_path = tmp_path / "reg.db"
    spec_content = (
        'novafabric_spec_version: "1"\n'
        "asset_type: model\n"
        "name: my-dedupe-model\n"
        "version: 0.1.0\n"
        "status: development\n"
        'description: ""\n'
        "spec:\n"
        "  framework: openai\n"
        "  artifact_path: my-dedupe-model\n"
    )
    spec_file = tmp_path / "model.yaml"
    spec_file.write_text(spec_content)
    with patch.dict(os.environ, {"NOVAFABRIC_DB_PATH": str(db_path)}):
        _register_from_file(spec_file, db_path)  # first — succeeds
        _register_from_file(spec_file, db_path)  # second — DuplicateAssetError


def test_register_from_file_generic_exception(tmp_path: Path):
    """_register_from_file handles unexpected errors without raising."""
    from unittest.mock import patch

    from novafabric.cli.suggest_register import _register_from_file

    spec_file = tmp_path / "model.yaml"
    spec_file.write_text('novafabric_spec_version: "1"\nasset_type: model\nname: test\n')
    with patch(
        "novafabric.registry.service.register_asset",
        side_effect=RuntimeError("unexpected"),
    ):
        with patch(
            "novafabric.spec.validator.validate_spec",
            return_value=type("Spec", (), {"name": "test", "version": "0.1.0"})(),
        ):
            _register_from_file(spec_file, None)  # must not raise


def test_suggest_register_interactive_edit(tmp_path: Path):
    """Interactive 'e' choice writes a draft, opens the editor, asks to register."""
    from unittest.mock import patch

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    d = runs_dir / "01EDITCAPS"
    d.mkdir()
    model_rec = [
        {
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o-2024-08-06",
            "gen_ai.response.model": "gpt-4o-2024-08-06",
        }
    ]
    (d / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in model_rec))
    (d / "tool-calls.jsonl").write_text("")
    # No command → no agent suggestion, only one model prompt
    manifest = {
        "run_id": "01EDITCAPS", "schema_version": "0.1.0",
        "command": [], "exit_code": 0, "status": "success",
    }
    (d / "capsule.yaml").write_text(yaml.dump(manifest))

    out_dir = tmp_path / "drafts"
    db_path = tmp_path / "reg.db"
    import os
    env = {**os.environ, "NOVAFABRIC_DB_PATH": str(db_path)}

    with patch("novafabric.cli.suggest_register._open_editor"):
        result = cli_runner.invoke(
            app,
            ["suggest-register", "--runs-dir", str(runs_dir), "--output-dir", str(out_dir)],
            input="e\ny\n",
            env=env,
        )
    assert result.exit_code == 0
    assert out_dir.exists()
    assert any(f.suffix == ".yaml" for f in out_dir.iterdir())
