from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_daemon_status_no_daemon(tmp_path, monkeypatch):
    monkeypatch.delenv("NOVAFABRIC_CAPTURE_SOCKET", raising=False)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0
    assert "not running" in result.stdout.lower()


def test_daemon_help_lists_subcommands():
    result = runner.invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0
    for sub in ("start", "stop", "status"):
        assert sub in result.stdout


def test_daemon_stop_no_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    result = runner.invoke(app, ["daemon", "stop"])
    assert result.exit_code == 0
    assert "not running" in result.stdout.lower()
