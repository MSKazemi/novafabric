from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_capture_no_daemon_runs_direct(tmp_path, monkeypatch):
    monkeypatch.delenv("NOVAFABRIC_CAPTURE_SOCKET", raising=False)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    result = runner.invoke(
        app, ["capture", "--no-daemon", "python", "-c", "print('hi')"]
    )
    assert result.exit_code == 0
    assert len(list((tmp_path / "capsules").glob("*"))) == 1


def test_capture_daemon_auto_falls_back_when_absent(tmp_path, monkeypatch):
    # Daemon flag defaults to auto; with no socket it must fall back to in-process
    # direct execution and still succeed (never block the workload).
    monkeypatch.delenv("NOVAFABRIC_CAPTURE_SOCKET", raising=False)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    result = runner.invoke(app, ["capture", "python", "-c", "print('hi')"])
    assert result.exit_code == 0
    assert len(list((tmp_path / "capsules").glob("*"))) == 1
