"""M1: the event-stream refs the orchestrator writes must be in every schema copy.

``capture/orchestrator.py::_event_stream_refs`` adds ``network_events_ref``,
``file_events_ref`` and ``human_approvals_ref`` to ``capsule.yaml`` whenever the
matching stream is non-empty. The capsule schema is closed at the top level
(``additionalProperties: false``), so before these fields were registered any
run that made one HTTP call through an instrumented client produced a capsule
that ``nova validate`` rejected. The two halves each had green tests; this file
spans them: golden fixtures against all three schema copies, the orchestrator's
own key set against the schema, and one real capture validated end to end.
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capture import orchestrator as orchestrator_mod
from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "capsule-event-stream-refs"
SCHEMA_COPIES = {
    "packaged": REPO_ROOT / "src" / "novafabric" / "schemas" / "run-capsule.schema.json",
    "root": REPO_ROOT / "schemas" / "run-capsule.schema.json",
    "dashboard": REPO_ROOT / "ui" / "dashboard" / "src" / "data" / "schemas" / "run-capsule.schema.json",
}
STREAM_REFS = {
    "network_events_ref": "network_events.jsonl",
    "file_events_ref": "file_events.jsonl",
    "human_approvals_ref": "human_approvals.jsonl",
}
_FIXTURES = sorted(p.name for p in FIXTURE_DIR.glob("*.json"))


def _validator(path: Path) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(path.read_text()))


def test_there_are_fixtures_of_both_kinds() -> None:
    assert sum(f.startswith("valid-") for f in _FIXTURES) >= 3, _FIXTURES
    assert sum(f.startswith("invalid-") for f in _FIXTURES) >= 4, _FIXTURES


@pytest.mark.parametrize("copy", sorted(SCHEMA_COPIES))
@pytest.mark.parametrize("fixture", _FIXTURES)
def test_golden_fixture_behaves_as_named(copy: str, fixture: str) -> None:
    manifest = json.loads((FIXTURE_DIR / fixture).read_text())
    errors = list(_validator(SCHEMA_COPIES[copy]).iter_errors(manifest))
    if fixture.startswith("valid-"):
        assert not errors, [e.message for e in errors]
    else:
        assert errors, f"{fixture}: expected schema rejection, got none"


@pytest.mark.parametrize("copy", sorted(SCHEMA_COPIES))
def test_every_stream_ref_the_orchestrator_writes_is_registered(copy: str, tmp_path: Path) -> None:
    for filename in STREAM_REFS.values():
        (tmp_path / filename).write_text("{}\n")
    written = orchestrator_mod._event_stream_refs(tmp_path)
    assert written == STREAM_REFS, "orchestrator stream set changed — update STREAM_REFS"
    props = json.loads(SCHEMA_COPIES[copy].read_text())["properties"]
    for key, filename in written.items():
        assert key in props, f"{key} missing from the {copy} schema"
        assert props[key].get("const") == filename


def test_stream_refs_stay_optional() -> None:
    for path in SCHEMA_COPIES.values():
        required = set(json.loads(path.read_text())["required"])
        assert not required & set(STREAM_REFS), path


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def local_http_url() -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_capsule_with_a_network_event_passes_nova_validate(
    tmp_path: Path, local_http_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("requests")
    # Hooks record a NetworkEvent only for a known model endpoint; an Ollama
    # base URL is the documented way to make a local server one.
    monkeypatch.setenv("OLLAMA_BASE_URL", local_http_url.rstrip("/"))
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    code = f"import requests; requests.get({local_http_url!r}, timeout=10)"
    result = orch.run([sys.executable, "-c", code])
    assert result.exit_code == 0

    manifest = yaml.safe_load((result.capsule_dir / "capsule.yaml").read_text())
    assert manifest.get("network_events_ref") == "network_events.jsonl", (
        "the requests hook did not record a network event — the end-to-end "
        "check below would be vacuous"
    )

    out = CliRunner().invoke(app, ["validate", str(result.capsule_dir)])
    assert isinstance(out.exception, SystemExit) or out.exception is None, out.exception
    assert out.exit_code == 0, out.output
