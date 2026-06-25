import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "test.db"
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    return db


def test_help_shows_all_commands(cli_env: Path) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    cmds = [
        "register", "list", "inspect", "promote",
        "eval", "diff", "report", "validate",
    ]
    for cmd in cmds:
        assert cmd in result.output


def test_register_valid_model(cli_env: Path, fixtures_dir: Path) -> None:
    result = runner.invoke(app, ["register", str(fixtures_dir / "valid_model.yaml")])
    assert result.exit_code == 0
    assert "fraud-model" in result.output
    assert "1.0.0" in result.output


def test_register_missing_field_exits_1(cli_env: Path, fixtures_dir: Path) -> None:
    result = runner.invoke(
        app, ["register", str(fixtures_dir / "missing_asset_type.yaml")]
    )
    assert result.exit_code == 1
    assert "asset_type" in result.output


def test_register_duplicate_exits_1(cli_env: Path, fixtures_dir: Path) -> None:
    yaml_path = str(fixtures_dir / "duplicate_test.yaml")
    runner.invoke(app, ["register", yaml_path])
    result = runner.invoke(app, ["register", yaml_path])
    assert result.exit_code == 1
    out = result.output.lower()
    assert "already registered" in out or "duplicate" in out


def test_list_with_type_filter(cli_env: Path, fixtures_dir: Path) -> None:
    runner.invoke(app, ["register", str(fixtures_dir / "valid_model.yaml")])
    runner.invoke(app, ["register", str(fixtures_dir / "valid_agent.yaml")])
    result = runner.invoke(app, ["list", "--type", "model"])
    assert result.exit_code == 0
    assert "fraud-model" in result.output
    assert "kube-rca-agent" not in result.output


def test_inspect_full_metadata(cli_env: Path, fixtures_dir: Path) -> None:
    runner.invoke(app, ["register", str(fixtures_dir / "valid_model.yaml")])
    result = runner.invoke(app, ["inspect", "fraud-model@1.0.0"])
    assert result.exit_code == 0
    assert "fraud-model" in result.output
    assert "status" in result.output
    assert "created_at" in result.output


def test_promote_model_lifecycle(cli_env: Path, fixtures_dir: Path) -> None:
    runner.invoke(app, ["register", str(fixtures_dir / "valid_model.yaml")])
    result = runner.invoke(
        app, ["promote", "direct", "fraud-model@1.0.0", "--to", "staging"]
    )
    assert result.exit_code == 0
    inspect_result = runner.invoke(app, ["inspect", "fraud-model@1.0.0"])
    assert "staging" in inspect_result.output


def test_promote_agent_without_eval_exits_1(cli_env: Path, fixtures_dir: Path) -> None:
    runner.invoke(app, ["register", str(fixtures_dir / "valid_agent.yaml")])
    result = runner.invoke(
        app, ["promote", "direct", "kube-rca-agent@v1.0.0", "--to", "staging"]
    )
    assert result.exit_code == 1
    assert "eval" in result.output.lower()


def test_validate_valid_spec_exits_0(cli_env: Path, fixtures_dir: Path) -> None:
    result = runner.invoke(app, ["validate", str(fixtures_dir / "valid_model.yaml")])
    assert result.exit_code == 0


def test_validate_invalid_spec_exits_1(cli_env: Path, fixtures_dir: Path) -> None:
    result = runner.invoke(
        app, ["validate", str(fixtures_dir / "missing_asset_type.yaml")]
    )
    assert result.exit_code == 1


def test_diff_shows_changed_field(cli_env: Path, tmp_path: Path) -> None:
    v1 = tmp_path / "agent_v1.yaml"
    v2 = tmp_path / "agent_v2.yaml"
    base = (
        "novafabric_spec_version: '1'\n"
        "asset_type: agent\n"
        "name: diff-agent\n"
        "status: development\n"
        "spec:\n"
        "  model:\n"
        "    provider: anthropic\n"
        "    name: claude-sonnet-4-6\n"
        "    temperature: {temp}\n"
        "  tools: []\n"
        "  prompts: {{}}\n"
        "  policies: []\n"
        "  evals:\n"
        "    - suite_a\n"
    )
    v1.write_text(base.format(temp="0.1") + "version: v1.0.0\n")
    v2.write_text(base.format(temp="0.9") + "version: v2.0.0\n")
    runner.invoke(app, ["register", str(v1)])
    runner.invoke(app, ["register", str(v2)])
    result = runner.invoke(app, ["diff", "diff-agent@v1.0.0", "diff-agent@v2.0.0"])
    assert result.exit_code == 0
    assert "temperature" in result.output


def test_validate_missing_file_exits_1(cli_env: Path) -> None:
    result = runner.invoke(app, ["validate", "/tmp/does_not_exist_nf_xyz.yaml"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_validate_invalid_yaml_exits_1(cli_env: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("key: [\ninvalid yaml")
    result = runner.invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1
    assert "yaml" in result.output.lower()


def test_promote_force_agent(cli_env: Path, fixtures_dir: Path) -> None:
    runner.invoke(app, ["register", str(fixtures_dir / "valid_agent.yaml")])
    runner.invoke(app, ["eval", "agent", "kube-rca-agent@v1.0.0"])
    result = runner.invoke(
        app,
        ["promote", "direct", "kube-rca-agent@v1.0.0", "--to", "staging", "--force"],
        input="kube-rca-agent\n",
    )
    assert result.exit_code == 0
    assert "Promoted" in result.output


def test_report_markdown_output(cli_env: Path, fixtures_dir: Path) -> None:
    runner.invoke(app, ["register", str(fixtures_dir / "valid_model.yaml")])
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "Inventory" in result.output


def test_report_json_output(cli_env: Path, fixtures_dir: Path) -> None:
    runner.invoke(app, ["register", str(fixtures_dir / "valid_model.yaml")])
    result = runner.invoke(app, ["report", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
