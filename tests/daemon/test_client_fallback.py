from novafabric.daemon import client


def test_resolve_socket_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_CAPTURE_SOCKET", str(tmp_path / "s.sock"))
    assert client.resolve_socket_path() == str(tmp_path / "s.sock")


def test_fallback_exec_argv_built():
    argv = client.build_fallback_argv(["echo", "hi"])
    assert argv[:3] == ["nova", "capture", "--no-daemon"]
    assert argv[-2:] == ["echo", "hi"]


def test_connect_returns_none_when_socket_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_CAPTURE_SOCKET", str(tmp_path / "absent.sock"))
    assert client.try_connect() is None
