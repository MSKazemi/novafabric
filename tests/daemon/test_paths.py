from novafabric._paths import daemon_run_dir, daemon_socket_path


def test_daemon_paths_under_nova_home(tmp_path, monkeypatch):
    monkeypatch.delenv("NOVAFABRIC_CAPTURE_SOCKET", raising=False)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    assert daemon_run_dir() == tmp_path / "run"
    assert daemon_socket_path() == tmp_path / "run" / "capture.sock"


def test_daemon_socket_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_CAPTURE_SOCKET", str(tmp_path / "custom.sock"))
    assert daemon_socket_path() == tmp_path / "custom.sock"
