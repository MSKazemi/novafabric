import sys

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_capture_no_daemon_runs_direct(tmp_path, monkeypatch):
    monkeypatch.delenv("NOVAFABRIC_CAPTURE_SOCKET", raising=False)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    result = runner.invoke(
        app, ["capture", "--no-daemon", sys.executable, "-c", "print('hi')"]
    )
    assert result.exit_code == 0
    assert len(list((tmp_path / "capsules").glob("*"))) == 1


def test_capture_daemon_auto_falls_back_when_absent(tmp_path, monkeypatch):
    # Daemon flag defaults to auto; with no socket it must fall back to in-process
    # direct execution and still succeed (never block the workload).
    monkeypatch.delenv("NOVAFABRIC_CAPTURE_SOCKET", raising=False)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    result = runner.invoke(app, ["capture", sys.executable, "-c", "print('hi')"])
    assert result.exit_code == 0
    assert len(list((tmp_path / "capsules").glob("*"))) == 1


def test_capture_runs_in_process_when_daemon_up_but_novacap_missing(tmp_path, monkeypatch):
    """M10: a reachable daemon plus a missing ``novacap`` must not traceback —
    the workload runs in-process with a warning (never block the workload)."""
    import socket

    from novafabric.cli import capture as capture_mod

    sock_path = tmp_path / "d.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(4)
    monkeypatch.setenv("NOVAFABRIC_CAPTURE_SOCKET", str(sock_path))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    tried: list[str] = []

    def missing_execvp(file, args):
        tried.append(file)
        raise FileNotFoundError(2, "No such file or directory", file)

    monkeypatch.setattr(capture_mod.os, "execvp", missing_execvp)
    try:
        result = runner.invoke(app, ["capture", sys.executable, "-c", "print('hi')"])
    finally:
        listener.close()
    assert tried == ["novacap"], "the daemon path must still be attempted first"
    assert isinstance(result.exception, SystemExit) or result.exception is None, result.exception
    assert result.exit_code == 0, result.output
    assert "novacap" in result.output
    assert len(list((tmp_path / "capsules").glob("*"))) == 1
