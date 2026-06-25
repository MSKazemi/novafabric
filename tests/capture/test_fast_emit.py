"""Tests for ``--fast-emit`` — import-deferred hook installation (ADR-0092 slice B).

Covers three layers:

1. the generic ``sys.meta_path`` post-import mechanism
   (:mod:`novafabric.capture.hooks._deferred`);
2. :func:`novafabric.capture.hooks.install_all_deferred` wiring the built-in
   hooks through that mechanism (defer-then-install, fail-open);
3. end-to-end propagation through the orchestrator + sitecustomize loader,
   including a real-subprocess fidelity check that a deferred-install capsule
   records the same network event as the eager path.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from novafabric.capture import hooks
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import (
    get_current_recorder,
    set_current_recorder,
)
from novafabric.capture.hooks import _deferred


@pytest.fixture(autouse=True)
def _clean_meta_path_and_recorder():
    """Keep ``sys.meta_path`` and the recorder singleton clean between tests."""
    _deferred._reset_for_testing()
    set_current_recorder(None)
    hooks._installed.clear()
    yield
    _deferred._reset_for_testing()
    set_current_recorder(None)
    hooks._installed.clear()


def _make_module(tmp_path: Path, name: str, body: str) -> None:
    """Write an importable module ``name`` under ``tmp_path`` (added to sys.path)."""
    (tmp_path / f"{name}.py").write_text(body)
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))


def _forget(tmp_path: Path, *names: str) -> None:
    for n in names:
        sys.modules.pop(n, None)
    if str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))


# --------------------------------------------------------------------------- #
# Layer 1 — the generic post-import mechanism
# --------------------------------------------------------------------------- #

def test_callback_fires_on_import(tmp_path: Path) -> None:
    _make_module(tmp_path, "fe_mod_a", "VALUE = 7\n")
    fired: list[int] = []
    _deferred.register_post_import_hook("fe_mod_a", lambda m: fired.append(m.VALUE))

    assert fired == []  # not imported yet → callback dormant
    assert "fe_mod_a" not in sys.modules

    import fe_mod_a  # noqa: F401  (workload imports it → callback fires)

    assert fired == [7]
    _forget(tmp_path, "fe_mod_a")


def test_unused_target_is_never_imported(tmp_path: Path) -> None:
    called: list[int] = []
    _deferred.register_post_import_hook("fe_never_xyz", lambda m: called.append(1))
    # The whole point: an unused SDK is never imported just to register a hook.
    assert "fe_never_xyz" not in sys.modules
    assert called == []


def test_already_imported_fires_immediately(tmp_path: Path) -> None:
    _make_module(tmp_path, "fe_mod_b", "VALUE = 9\n")
    import fe_mod_b  # noqa: F401  imported BEFORE registration

    fired: list[int] = []
    _deferred.register_post_import_hook("fe_mod_b", lambda m: fired.append(m.VALUE))
    assert fired == [9]  # fired immediately — no import missed
    _forget(tmp_path, "fe_mod_b")


def test_callback_exception_does_not_break_workload_import(tmp_path: Path) -> None:
    _make_module(tmp_path, "fe_mod_c", "VALUE = 1\n")

    def boom(_m: object) -> None:
        raise RuntimeError("hook install failed")

    _deferred.register_post_import_hook("fe_mod_c", boom)
    import fe_mod_c  # must NOT raise — fail-open

    assert fe_mod_c.VALUE == 1
    _forget(tmp_path, "fe_mod_c")


def test_one_shot_not_rewrapped_on_reimport(tmp_path: Path) -> None:
    _make_module(tmp_path, "fe_mod_d", "VALUE = 5\n")
    count: list[int] = []
    _deferred.register_post_import_hook("fe_mod_d", lambda m: count.append(1))

    import fe_mod_d  # noqa: F401
    del sys.modules["fe_mod_d"]
    import fe_mod_d  # noqa: F401,F811  re-import → callback already consumed

    assert count == [1]
    _forget(tmp_path, "fe_mod_d")


def test_reset_for_testing_removes_finder(tmp_path: Path) -> None:
    _deferred.register_post_import_hook("fe_some_target", lambda m: None)
    assert any(f is _deferred._finder for f in sys.meta_path)
    _deferred._reset_for_testing()
    assert _deferred._finder is None


def test_already_imported_callback_exception_swallowed(tmp_path: Path) -> None:
    _make_module(tmp_path, "fe_mod_e", "VALUE = 3\n")
    import fe_mod_e  # noqa: F401  already imported

    def boom(_m: object) -> None:
        raise RuntimeError("boom")

    # Must not raise even though the immediate callback raises (fail-open).
    _deferred.register_post_import_hook("fe_mod_e", boom)
    _forget(tmp_path, "fe_mod_e")


def test_post_import_loader_forwards_attrs_and_create_module() -> None:
    class _RealLoader:
        flag = "real"

        def create_module(self, spec: object) -> str:
            return "made"

        def exec_module(self, module: object) -> None:
            pass

        def get_filename(self, name: str) -> str:
            return f"/x/{name}.py"

    loader = _deferred._PostImportLoader(_RealLoader(), lambda m: None)
    # __getattr__ forwards unknown attributes/methods to the real loader.
    assert loader.flag == "real"
    assert loader.get_filename("m") == "/x/m.py"
    # create_module delegates to the real loader when present.
    assert loader.create_module(object()) == "made"


def test_post_import_loader_create_module_without_real_returns_none() -> None:
    class _BareLoader:
        def exec_module(self, module: object) -> None:
            pass

    loader = _deferred._PostImportLoader(_BareLoader(), lambda m: None)
    assert loader.create_module(object()) is None


# --------------------------------------------------------------------------- #
# Layer 2 — install_all_deferred wiring the built-in hooks
# --------------------------------------------------------------------------- #

def test_install_all_deferred_defers_then_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fake SDK is not imported by registration; importing it installs the hook
    and lazily sets the recorder singleton — proving fidelity of the lazy path."""
    _make_module(tmp_path, "fe_sdk_q", "LOADED = True\n")
    _make_module(
        tmp_path,
        "fe_hook_q",
        textwrap.dedent(
            """\
            class FakeHook:
                installed = False
                def __init__(self, writer, parent_span_id):
                    self.writer = writer
                    self.parent_span_id = parent_span_id
                def install(self):
                    FakeHook.installed = True
                def uninstall(self):
                    FakeHook.installed = False
            """
        ),
    )
    monkeypatch.setattr(
        hooks, "_BUILT_IN_HOOKS", (("fe_sdk_q", "fe_hook_q", "FakeHook"),)
    )

    cap = tmp_path / "run-q"
    cap.mkdir()
    writer = CapsuleWriter(run_id="run-q", base_dir=tmp_path)
    writer._dir = cap

    hooks.install_all_deferred(writer, parent_span_id="0" * 16)

    import fe_hook_q  # bring the hook class into scope for assertions

    assert fe_hook_q.FakeHook.installed is False  # not installed yet
    assert "fe_sdk_q" not in sys.modules  # SDK not force-imported
    assert get_current_recorder() is None  # recorder deferred too

    import fe_sdk_q  # noqa: F401  workload imports the SDK → hook fires

    assert fe_hook_q.FakeHook.installed is True
    assert get_current_recorder() is not None  # recorder set lazily before events

    hooks.uninstall_all()
    assert fe_hook_q.FakeHook.installed is False
    _forget(tmp_path, "fe_sdk_q", "fe_hook_q")


def test_install_all_deferred_eager_for_already_loaded_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the target SDK is already imported, the hook installs immediately."""
    _make_module(tmp_path, "fe_sdk_r", "LOADED = True\n")
    _make_module(
        tmp_path,
        "fe_hook_r",
        textwrap.dedent(
            """\
            class FakeHook:
                installed = False
                def __init__(self, writer, parent_span_id):
                    pass
                def install(self):
                    FakeHook.installed = True
                def uninstall(self):
                    FakeHook.installed = False
            """
        ),
    )
    import fe_sdk_r  # noqa: F401  already imported before install_all_deferred
    monkeypatch.setattr(
        hooks, "_BUILT_IN_HOOKS", (("fe_sdk_r", "fe_hook_r", "FakeHook"),)
    )

    cap = tmp_path / "run-r"
    cap.mkdir()
    writer = CapsuleWriter(run_id="run-r", base_dir=tmp_path)
    writer._dir = cap

    hooks.install_all_deferred(writer, parent_span_id="0" * 16)

    import fe_hook_r

    assert fe_hook_r.FakeHook.installed is True  # installed eagerly
    hooks.uninstall_all()
    _forget(tmp_path, "fe_sdk_r", "fe_hook_r")


def test_install_all_deferred_survives_broken_hook_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook module that fails to import must not break the workload's SDK import."""
    _make_module(tmp_path, "fe_sdk_s", "LOADED = True\n")
    monkeypatch.setattr(
        hooks, "_BUILT_IN_HOOKS", (("fe_sdk_s", "fe_hook_does_not_exist", "Nope"),)
    )
    cap = tmp_path / "run-s"
    cap.mkdir()
    writer = CapsuleWriter(run_id="run-s", base_dir=tmp_path)
    writer._dir = cap

    hooks.install_all_deferred(writer, parent_span_id="0" * 16)
    import fe_sdk_s  # must NOT raise even though the hook module is missing

    assert fe_sdk_s.LOADED is True
    _forget(tmp_path, "fe_sdk_s")


# --------------------------------------------------------------------------- #
# Layer 3 — orchestrator + sitecustomize end-to-end
# --------------------------------------------------------------------------- #

def test_orchestrator_sets_fast_emit_env(tmp_path: Path) -> None:
    """fast_emit=True propagates NOVAFABRIC_FAST_EMIT=1 to the workload subprocess."""
    from novafabric.capture.orchestrator import CaptureOrchestrator

    probe = tmp_path / "fe.txt"
    script = tmp_path / "probe.py"
    script.write_text(
        f"import os\n"
        f"open({str(probe)!r}, 'w').write(os.environ.get('NOVAFABRIC_FAST_EMIT', 'unset'))\n"
    )

    orch = CaptureOrchestrator(base_dir=tmp_path / "runs", fast_emit=True)
    result = orch.run(command=[sys.executable, str(script)])
    assert result.exit_code == 0
    assert probe.read_text() == "1"


def test_orchestrator_no_fast_emit_env_by_default(tmp_path: Path) -> None:
    from novafabric.capture.orchestrator import CaptureOrchestrator

    probe = tmp_path / "fe.txt"
    script = tmp_path / "probe.py"
    script.write_text(
        f"import os\n"
        f"open({str(probe)!r}, 'w').write(os.environ.get('NOVAFABRIC_FAST_EMIT', 'unset'))\n"
    )

    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")  # default: fast_emit=False
    result = orch.run(command=[sys.executable, str(script)])
    assert result.exit_code == 0
    assert probe.read_text() == "unset"


def _run_capture(tmp_path: Path, *, fast_emit: bool, port: int) -> Path:
    """Run a workload that imports requests and GETs a local 'ollama' endpoint."""
    from novafabric.capture.orchestrator import CaptureOrchestrator

    script = tmp_path / f"wl_{'fast' if fast_emit else 'eager'}.py"
    script.write_text(
        textwrap.dedent(
            f"""\
            import requests
            requests.get("http://127.0.0.1:{port}/api/tags", timeout=5)
            """
        )
    )
    orch = CaptureOrchestrator(base_dir=tmp_path / f"runs_{fast_emit}", fast_emit=fast_emit)
    result = orch.run(command=[sys.executable, str(script)])
    assert result.exit_code == 0, result.stderr
    return Path(result.capsule_dir)


def test_fast_emit_fidelity_records_network_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real-subprocess fidelity: a --fast-emit run records the same network event
    as an eager run. Uses a local HTTP server registered as the Ollama endpoint
    (so the requests hook treats the URL as a known AI endpoint and records it)."""
    requests = pytest.importorskip("requests")  # noqa: F841
    import http.server
    import socket
    import threading

    # Bind a free port for a tiny local "ollama" server.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"models": []}')

        def log_message(self, *_a: object) -> None:  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Make the local server a *known* AI endpoint for the URL registry.
    monkeypatch.setenv("OLLAMA_BASE_URL", f"http://127.0.0.1:{port}")
    try:
        eager_cap = _run_capture(tmp_path, fast_emit=False, port=port)
        fast_cap = _run_capture(tmp_path, fast_emit=True, port=port)
    finally:
        server.shutdown()
        server.server_close()

    eager_events = (eager_cap / "network_events.jsonl")
    fast_events = (fast_cap / "network_events.jsonl")
    assert eager_events.exists() and eager_events.read_text().strip(), (
        "eager path should record a network event for the known AI URL"
    )
    assert fast_events.exists() and fast_events.read_text().strip(), (
        "--fast-emit path should record the same network event (fidelity)"
    )
