"""No ``$ref`` is ever retrieved while validating a schema NovaFabric did not ship.

jsonschema (>= 4.18) resolves an unknown ``$ref`` URI by default through a
deprecated registry that calls ``urllib.request.urlopen`` on it. A tool schema
with a *nested* remote ref (``{"properties": {"x": {"$ref": "http://..."}}}``)
therefore made capture issue an HTTP GET, at every capture level including
``air_gapped``, although the product promises that remote schema refs are never
fetched. ``file://`` refs were read the same way.

These tests stand up a local HTTP server that records every request and assert
it receives **none**. The server answers any GET with a schema that would make
the payload *valid*, so a fetch would also show up as a wrong verdict
(``true`` instead of ``null``).
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric._jsonschema_offline import (
    SchemaRefUnresolved,
    offline_validator,
)
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.schema_validation import (
    compute_verdict,
    revalidate_tool_calls,
    validate_capsule_tool_calls,
)
from novafabric.eval.offline import ContractSchemaError, run_contract

RUN_ID = "01HXNOEGRESS0000000000000A"
_PROXY_VARS = (
    "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
)


class _RefServer:
    """Records the path of every request it receives."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — http.server API
                outer.requests.append(self.path)
                body = json.dumps({"type": "integer"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/schema+json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # silence stderr
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _RefServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def ref_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RefServer]:
    # A proxy env var would route a fetch away from this server and hide it.
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    with _RefServer() as server:
        yield server


def _record(args_ref: str | None, *, result_ref: str | None = None) -> dict[str, Any]:
    return {
        "tool_call_id": "01HXNOEGRESSCALL000000000A",
        "tool_name": "lookup",
        "arguments": {"x": 7},
        "arguments_schema_ref": args_ref,
        "result": {"x": 7},
        "result_schema_ref": result_ref,
        "status": "success",
    }


def _nested_remote(base: str) -> dict[str, Any]:
    return {"type": "object", "properties": {"x": {"$ref": f"{base}/remote.json"}}}


def _write_schema(directory: Path, name: str, schema: Any) -> str:
    (directory / name).write_text(json.dumps(schema))
    return name


# ── capture-time verdict (ADR-0128 validator core) ──────────────────────────


@pytest.mark.parametrize(
    "schema_for",
    [
        pytest.param(_nested_remote, id="nested-http-ref"),
        pytest.param(
            lambda b: {"$id": f"{b}/base.json",
                       "properties": {"x": {"$ref": "sibling.json"}}},
            id="id-relative-ref",
        ),
        pytest.param(
            lambda b: {"properties": {"x": {"$dynamicRef": f"{b}/dyn.json"}}},
            id="dynamic-ref",
        ),
        pytest.param(
            lambda b: {"$defs": {"a": {"$ref": f"{b}/deep.json"}},
                       "properties": {"x": {"$ref": "#/$defs/a"}}},
            id="ref-via-local-defs",
        ),
    ],
)
def test_nested_remote_ref_is_never_fetched(
    tmp_path: Path, ref_server: _RefServer, schema_for: Any
) -> None:
    ref = _write_schema(tmp_path, "args.schema.json", schema_for(ref_server.base))

    verdict = compute_verdict(_record(ref), tmp_path)

    assert ref_server.requests == []
    assert verdict is not None
    assert verdict["arguments_valid"] is None  # unvalidated, never "valid"
    [err] = verdict["errors"]
    assert err["keyword"] == "schema-unresolved"
    assert err["target"] == "arguments"
    assert "not retrieved" in err["message"]


def test_capture_path_records_and_never_fetches(
    tmp_path: Path, ref_server: _RefServer
) -> None:
    """The real capture write path: the record lands, flagged, with no egress."""
    writer = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    writer.open()
    ref = _write_schema(writer.capsule_dir, "args.schema.json",
                        _nested_remote(ref_server.base))

    writer.append_tool_call(_record(ref))

    assert ref_server.requests == []
    [line] = (writer.capsule_dir / "tool-calls.jsonl").read_text().splitlines()
    verdict = json.loads(line)["schema_validation"]
    assert verdict["arguments_valid"] is None
    assert verdict["errors"][0]["keyword"] == "schema-unresolved"


def test_partial_errors_before_an_unresolved_ref_are_not_kept(
    tmp_path: Path, ref_server: _RefServer
) -> None:
    """A verdict of ``null`` must not carry violations from a half-run pass."""
    schema = {
        "type": "object",
        "required": ["missing"],
        "properties": {"x": {"$ref": f"{ref_server.base}/remote.json"}},
    }
    ref = _write_schema(tmp_path, "args.schema.json", schema)

    verdict = compute_verdict(_record(ref), tmp_path)

    assert ref_server.requests == []
    assert verdict is not None
    assert verdict["arguments_valid"] is None
    assert [e["keyword"] for e in verdict["errors"]] == ["schema-unresolved"]


def test_file_uri_ref_is_not_read(tmp_path: Path) -> None:
    """``file://`` refs were opened by urlopen too (``file:///dev/zero`` never ends)."""
    target = tmp_path / "integer.json"
    target.write_text(json.dumps({"type": "integer"}))
    schema = {"properties": {"x": {"$ref": target.as_uri()}}}
    ref = _write_schema(tmp_path, "args.schema.json", schema)

    verdict = compute_verdict(_record(ref), tmp_path)

    assert verdict is not None
    assert verdict["arguments_valid"] is None
    assert verdict["errors"][0]["keyword"] == "schema-unresolved"


def test_report_and_replay_revalidation_never_fetch(
    tmp_path: Path, ref_server: _RefServer
) -> None:
    ref = _write_schema(tmp_path, "args.schema.json", _nested_remote(ref_server.base))
    record = _record(ref)
    (tmp_path / "tool-calls.jsonl").write_text(json.dumps(record) + "\n")

    report = validate_capsule_tool_calls(tmp_path)
    drift = revalidate_tool_calls([record], tmp_path)

    assert ref_server.requests == []
    assert report.unresolved == 1
    assert report.violations == []
    assert drift == []  # unresolved is null, never drift


# ── local refs keep working (no regression) ─────────────────────────────────


def test_local_defs_ref_still_validates(tmp_path: Path) -> None:
    schema = {
        "$defs": {"int": {"type": "integer"}},
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/int"}},
    }
    ref = _write_schema(tmp_path, "args.schema.json", schema)

    ok = compute_verdict(_record(ref), tmp_path)
    bad_record = _record(ref)
    bad_record["arguments"] = {"x": "seven"}
    bad = compute_verdict(bad_record, tmp_path)

    assert ok is not None and ok["arguments_valid"] is True and ok["errors"] == []
    assert bad is not None and bad["arguments_valid"] is False
    assert bad["errors"][0]["keyword"] == "type"


def test_id_scoped_local_ref_resolves_inside_the_document(tmp_path: Path) -> None:
    """An ``$id`` with an embedded subschema resolves without retrieval."""
    schema = {
        "$id": "https://schemas.invalid/root.json",
        "$defs": {"int": {"$id": "int.json", "type": "integer"}},
        "properties": {"x": {"$ref": "int.json"}},
    }
    ref = _write_schema(tmp_path, "args.schema.json", schema)

    verdict = compute_verdict(_record(ref), tmp_path)

    assert verdict is not None and verdict["arguments_valid"] is True


def test_bundled_meta_schema_ref_resolves_offline() -> None:
    """The JSON Schema meta-schemas ship with jsonschema; referencing them is local."""
    validator = offline_validator(
        {"$ref": "https://json-schema.org/draft/2020-12/schema"}
    )
    assert validator.is_valid({"type": "string"})
    assert not validator.is_valid({"type": 12})


def test_dangling_local_pointer_is_unresolved(tmp_path: Path) -> None:
    schema = {"properties": {"x": {"$ref": "#/$defs/missing"}}}
    ref = _write_schema(tmp_path, "args.schema.json", schema)

    verdict = compute_verdict(_record(ref), tmp_path)

    assert verdict is not None and verdict["arguments_valid"] is None
    assert verdict["errors"][0]["keyword"] == "schema-unresolved"


# ── the shared helper ───────────────────────────────────────────────────────


def test_helper_raises_named_error_without_fetching(ref_server: _RefServer) -> None:
    validator = offline_validator(_nested_remote(ref_server.base))

    with pytest.raises(SchemaRefUnresolved):
        list(validator.iter_errors({"x": 1}))
    assert ref_server.requests == []


def test_helper_passes_format_checker_through() -> None:
    import jsonschema  # type: ignore[import-untyped]

    schema = {"type": "string", "format": "date"}
    assert offline_validator(schema).is_valid("not-a-date")  # format is annotation
    checked = offline_validator(schema, format_checker=jsonschema.FormatChecker())
    assert not checked.is_valid("not-a-date")


# ── nova eval offline --check contract (user-supplied schema) ───────────────


def _contract_capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "tool-calls.jsonl").write_text('{"tool_name": "a", "output": {"x": 1}}\n')
    return cap


def test_run_contract_refuses_remote_ref_without_fetching(
    tmp_path: Path, ref_server: _RefServer
) -> None:
    cap = _contract_capsule(tmp_path)

    with pytest.raises(ContractSchemaError, match="not retrieved"):
        run_contract(cap, _nested_remote(ref_server.base))
    assert ref_server.requests == []


def test_run_contract_local_ref_still_scores(tmp_path: Path) -> None:
    cap = _contract_capsule(tmp_path)
    schema = {"$defs": {"i": {"type": "integer"}},
              "properties": {"x": {"$ref": "#/$defs/i"}}}

    assert run_contract(cap, schema).value == 1.0


def test_eval_offline_cli_exits_2_on_remote_ref_without_fetching(
    tmp_path: Path, ref_server: _RefServer
) -> None:
    from novafabric.cli.main import app

    cap = _contract_capsule(tmp_path)
    schema_file = tmp_path / "contract.schema.json"
    schema_file.write_text(json.dumps(_nested_remote(ref_server.base)))

    result = CliRunner().invoke(
        app,
        ["eval", "offline", "--capsule", str(cap), "--check", "contract",
         "--schema", str(schema_file)],
    )

    assert ref_server.requests == []
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)  # a usage error, not a traceback


# ── guards: one construction path, and nothing shipped needs retrieval ──────

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "novafabric"
_HELPER = _SRC / "_jsonschema_offline.py"


def test_no_validator_is_built_outside_the_offline_helper() -> None:
    """A bare ``Draft*Validator(...)`` gets jsonschema's retrieving registry."""
    pattern = re.compile(r"\bDraft\d+Validator\s*\(")
    offenders = [
        f"{path.relative_to(_REPO)}:{lineno}"
        for path in sorted(_SRC.rglob("*.py"))
        if path != _HELPER
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == [], (
        "build validators with novafabric._jsonschema_offline.offline_validator: "
        f"{offenders}"
    )


def _non_local_refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("$ref", "$dynamicRef") and isinstance(value, str):
                if not value.startswith("#"):
                    yield value
            else:
                yield from _non_local_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _non_local_refs(item)


def test_packaged_schemas_never_depend_on_ref_retrieval() -> None:
    """Every shipped schema validates offline, so the helper changes no verdict."""
    found: dict[str, list[str]] = {}
    for root in (_SRC / "schemas", _REPO / "schemas"):
        for path in sorted(root.rglob("*.json")):
            refs = sorted(set(_non_local_refs(json.loads(path.read_text(encoding="utf-8")))))
            if refs:
                found[str(path.relative_to(_REPO))] = refs
    assert found == {}
