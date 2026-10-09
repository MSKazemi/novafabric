"""ADR-0306 slice 1: the capture side of ``novafabric.capture.record.tool``.

Binding and canonicalisation, ``ignore``, the JSON result codec and its
not-servable reasons, the size cap, exception capture, nested-record marking
through the single ``CapsuleWriter`` write path, the no-op path outside capture
(and its overhead), sync and async functions, generator refusal, and the golden
fixtures under ``tests/fixtures/tool-calls/``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.capture import _tool_codec as codec
from novafabric.capture import record
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.hooks import _BUILT_IN_HOOKS
from novafabric.capture.hooks._python_tool import PAYLOADS_OFF_REASON, PythonToolHook
from novafabric.replay._contract import (
    TOOL_SURFACE_PYTHON,
    is_servable_tool_record,
    normalized_arg_hash,
    not_servable_reason,
    tool_surface,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "tool-calls"
_TOOL_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "tool-call.schema.json").read_text()
)


def _coverage_active() -> bool:
    coverage = sys.modules.get("coverage")
    return coverage is not None and coverage.Coverage.current() is not None


class _ListWriter:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append_tool_call(self, rec: dict[str, Any]) -> None:
        self.records.append(rec)


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ListWriter]:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    monkeypatch.delenv(codec.RESULT_MAX_BYTES_ENV, raising=False)
    writer = _ListWriter()
    hook = PythonToolHook(writer, "0123456789abcdef")  # type: ignore[arg-type]
    hook.install()
    try:
        yield writer
    finally:
        hook.uninstall()
    assert record._get_tool_handler() is None


@record.tool(mutation_class="read-only")
def add(a: int, b: int = 2, *rest: int, **opts: Any) -> dict[str, Any]:
    return {"sum": a + b + sum(rest), "opts": opts}


# ── no-op outside capture ────────────────────────────────────────────────────


def test_outside_capture_the_function_runs_and_nothing_is_written(tmp_path: Path) -> None:
    assert record._get_tool_handler() is None
    assert add(1) == {"sum": 3, "opts": {}}
    assert not list(tmp_path.rglob("*"))


def test_signature_docstring_and_annotations_are_preserved() -> None:
    @record.tool
    def documented(x: int) -> str:
        """Doc."""
        return str(x)

    import inspect

    assert documented.__doc__ == "Doc."
    assert documented.__name__ == "documented"
    assert inspect.signature(documented) == inspect.signature(documented.__wrapped__)
    assert list(inspect.signature(documented).parameters) == ["x"]
    assert documented.__novafabric_tool__.name == "documented"  # type: ignore[attr-defined]


def test_noop_overhead_is_negligible() -> None:
    """ADR-0306 D9: the no-op path is one call plus one global read.

    Asserted as a CPU-time ratio against ``get_current_recorder()`` -- the method
    of ``test_record_facade.py::test_noop_overhead_is_negligible`` -- with the
    decorated function's own (trivial) body included in the measured path.
    """
    from novafabric.capture.event_recorder import get_current_recorder

    @record.tool
    def trivial(x: int) -> int:
        return x

    n = 100_000

    def cpu_time(fn: Any, *args: Any) -> float:
        best = float("inf")
        for _ in range(3):
            t0 = time.process_time()
            for _ in range(n):
                fn(*args)
            best = min(best, time.process_time() - t0)
        return best

    global_read = cpu_time(get_current_recorder)
    noop = cpu_time(trivial, 1)
    assert noop < 3.0 * global_read, (
        f"{n} no-op decorated calls took {noop:.3f}s of CPU, "
        f"{noop / global_read:.1f}x the {global_read:.3f}s of {n} bare recorder reads"
    )
    if not _coverage_active():
        assert noop < 1.0, f"{n} no-op calls took {noop:.2f}s of CPU time"


# ── decoration-time refusals ─────────────────────────────────────────────────


def test_generator_functions_are_refused() -> None:
    with pytest.raises(TypeError, match="generator"):
        @record.tool
        def gen() -> Iterator[int]:
            yield 1

    with pytest.raises(TypeError, match="generator"):
        @record.tool
        async def agen() -> Any:
            yield 1


def test_bad_mutation_class_and_unknown_ignore_names_are_refused() -> None:
    with pytest.raises(ValueError, match="mutation_class"):
        record.tool(mutation_class="dangerous")
    with pytest.raises(ValueError, match="not parameters"):
        @record.tool(ignore=("nope",))
        def f(x: int) -> int:
            return x
    with pytest.raises(TypeError, match="sequence of names"):
        record.tool(ignore="ctx")


# ── capture: the record ──────────────────────────────────────────────────────


def test_one_schema_valid_record_per_sync_call(sink: _ListWriter) -> None:
    assert add(1, b=5) == {"sum": 6, "opts": {}}
    (rec,) = sink.records
    jsonschema.validate(rec, _TOOL_SCHEMA)
    assert rec["transport"] == "python"
    assert rec["tool_name"] == "add"
    assert rec["tool_provider"] == f"python://{__name__}"
    assert rec["mutation_class"] == "read-only" and rec["mutates"] is False
    assert rec["arguments"] == {"a": 1, "b": 5, "rest": [], "opts": {}}
    assert rec["result"] == {"value": {"sum": 6, "opts": {}}}
    assert rec["status"] == "success" and rec["agent_call_id"] is None
    ext = rec["extensions"]
    assert ext[codec.TOOL_SURFACE_EXT] == "python.function"
    assert ext[codec.RESULT_CODEC_EXT] == "json-v1"
    # The kept value is the evidence; a digest is only a stand-in for a dropped one.
    assert codec.RESULT_DIGEST_EXT not in ext
    assert ext[codec.QUALNAME_EXT] == f"{__name__}:add"
    assert tool_surface(rec) == TOOL_SURFACE_PYTHON and is_servable_tool_record(rec)


def test_async_calls_are_recorded(sink: _ListWriter) -> None:
    @record.tool(name="lookup", version="2.0", mutation_class="none")
    async def lookup_order(order_id: str) -> list[Any]:
        await asyncio.sleep(0)
        return [order_id]

    assert asyncio.run(lookup_order("o-1")) == ["o-1"]
    (rec,) = sink.records
    assert rec["tool_name"] == "lookup" and rec["tool_version"] == "2.0"
    assert rec["result"] == {"value": ["o-1"]}


def test_positional_keyword_and_default_calls_bind_identically(sink: _ListWriter) -> None:
    add(1)
    add(a=1, b=2)
    add(1, 2)
    hashes = {normalized_arg_hash(r["arguments"]) for r in sink.records}
    assert len(hashes) == 1


def test_varargs_kwargs_pydantic_and_dataclass_arguments_are_canonical(
    sink: _ListWriter,
) -> None:
    from pydantic import BaseModel

    class Query(BaseModel):
        q: str
        limit: int = 3

    @dataclasses.dataclass
    class Page:
        n: int

    @record.tool
    def search(query: Query, page: Page, *tags: str, **extra: Any) -> dict[str, Any]:
        return {"ok": True}

    search(Query(q="x"), Page(2), "a", "b", lang="en")
    (rec,) = sink.records
    assert rec["arguments"] == {
        "query": {"q": "x", "limit": 3}, "page": {"n": 2},
        "tags": ["a", "b"], "extra": {"lang": "en"},
    }
    assert rec["extensions"][codec.RESULT_CODEC_EXT] == "json-v1"


def test_ignore_drops_the_argument_and_is_recorded(sink: _ListWriter) -> None:
    @record.tool(ignore=("ctx",))
    def tool_with_ctx(ctx: object, q: str) -> str:
        return q

    tool_with_ctx(object(), "hello")
    (rec,) = sink.records
    assert rec["arguments"] == {"q": "hello"}
    assert rec["extensions"][codec.IGNORED_ARGUMENTS_EXT] == ["ctx"]


def test_a_non_json_argument_names_the_parameter(sink: _ListWriter) -> None:
    @record.tool
    def tool_with_ctx(ctx: object, q: str) -> str:
        return q

    assert tool_with_ctx(object(), "hello") == "hello"
    (rec,) = sink.records
    reason = not_servable_reason(rec)
    assert reason is not None
    assert "argument `ctx`" in reason and "ignore=('ctx',)" in reason
    assert rec["arguments"] == {}


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ((1, 2), "tuple"),
        ({1, 2}, "is a set"),
        ({1: "a"}, "non-str key"),
        (object(), "is a object"),
        (float("nan"), "non-finite"),
        (b"bytes", "is a bytes"),
    ],
    ids=["tuple", "set", "int-key", "object", "nan", "bytes"],
)
def test_non_json_results_are_recorded_not_servable(
    sink: _ListWriter, value: Any, fragment: str
) -> None:
    @record.tool
    def returns() -> Any:
        return value

    assert returns() is value
    (rec,) = sink.records
    jsonschema.validate(rec, _TOOL_SCHEMA)
    assert rec["result"] is None
    assert rec["extensions"][codec.RESULT_CODEC_EXT] == "not-servable"
    assert fragment in rec["extensions"][codec.NOT_SERVABLE_REASON_EXT]


def test_results_over_the_size_cap_keep_neither_value_nor_digest(
    sink: _ListWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(codec.RESULT_MAX_BYTES_ENV, "10")

    @record.tool
    def big() -> str:
        return "x" * 100

    big()
    (rec,) = sink.records
    assert rec["result"] is None
    assert codec.RESULT_DIGEST_EXT not in rec["extensions"]  # redacting it is unbounded work
    assert "over the 10-byte cap" in rec["extensions"][codec.NOT_SERVABLE_REASON_EXT]


def test_the_payloads_off_result_digest_is_bounded(
    sink: _ListWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "standard")

    @record.tool
    def sized(n: int) -> str:
        return "y" * n

    sized(100)
    sized(codec.RESULT_DIGEST_MAX_BYTES + 1)
    small, large = sink.records
    assert small["extensions"][codec.RESULT_DIGEST_EXT].startswith("sha256:")
    assert codec.RESULT_DIGEST_EXT not in large["extensions"]


def test_default_size_cap_is_one_mebibyte(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(codec.RESULT_MAX_BYTES_ENV, raising=False)
    assert codec.result_max_bytes() == 1024 * 1024
    monkeypatch.setenv(codec.RESULT_MAX_BYTES_ENV, "not-a-number")
    assert codec.result_max_bytes() == 1024 * 1024


def test_payloads_off_records_digests_and_a_recapture_hint(
    sink: _ListWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "standard")
    add(41)
    (rec,) = sink.records
    jsonschema.validate(rec, _TOOL_SCHEMA)
    assert rec["arguments"] == {} and rec["result"] is None
    ext = rec["extensions"]
    assert ext[codec.ARGUMENTS_DIGEST_EXT] == normalized_arg_hash(
        {"a": 41, "b": 2, "rest": [], "opts": {}}
    )
    assert ext[codec.RESULT_DIGEST_EXT].startswith("sha256:")
    assert ext[codec.NOT_SERVABLE_REASON_EXT] == PAYLOADS_OFF_REASON
    assert "41" not in json.dumps(rec["arguments"])


@pytest.mark.parametrize(
    "token",
    ["AKIA" + "QZ7XK2M4PZT3W6RN", "ghp" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp"],
    ids=["aws-key", "github-token"],
)
def test_digests_are_taken_over_the_redacted_values_never_the_raw_secret(
    sink: _ListWriter, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """NF-166: a digest of a raw secret is an offline guessing oracle. Both digests
    are computed after ADR-0009 redaction, so the secret contributes only its
    ``[REDACTED:<rule>]`` placeholder."""
    import hashlib

    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "standard")

    @record.tool
    def login(user: str, token: str) -> dict[str, str]:
        return {"user": user, "echo": token}

    login("alice", token)
    (rec,) = sink.records
    ext = rec["extensions"]
    raw_args = {"user": "alice", "token": token}
    raw_result = {"user": "alice", "echo": token}

    def canon(v: Any) -> str:
        return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert ext[codec.ARGUMENTS_DIGEST_EXT] != normalized_arg_hash(raw_args)
    assert ext[codec.ARGUMENTS_DIGEST_EXT] == normalized_arg_hash(
        codec.redact_for_digest(raw_args)
    )
    assert ext[codec.RESULT_DIGEST_EXT] != "sha256:" + hashlib.sha256(
        canon(raw_result).encode()
    ).hexdigest()
    assert ext[codec.RESULT_DIGEST_EXT] == "sha256:" + hashlib.sha256(
        canon(codec.redact_for_digest(raw_result)).encode()
    ).hexdigest()
    assert "[REDACTED:" in codec.redact_for_digest(raw_args)["token"]
    # Redaction is idempotent, so a sealed (already-redacted) record hashes the same.
    assert codec.redacted_arguments_digest(
        codec.redact_for_digest(raw_args)
    ) == codec.redacted_arguments_digest(raw_args)


def test_exceptions_propagate_unchanged_and_are_recorded(sink: _ListWriter) -> None:
    class MyError(Exception):
        pass

    @record.tool
    def fails(kind: str) -> None:
        if kind == "value":
            raise ValueError("bad 1")
        raise MyError("custom")

    with pytest.raises(ValueError, match="bad 1"):
        fails("value")
    with pytest.raises(MyError):
        fails("custom")
    builtin_rec, custom_rec = sink.records
    for rec in (builtin_rec, custom_rec):
        jsonschema.validate(rec, _TOOL_SCHEMA)
        assert rec["status"] == "error" and rec["result"] is None
    assert builtin_rec["error"] == {"type": "ValueError", "message": "bad 1", "traceback_ref": None}
    assert builtin_rec["extensions"][codec.EXCEPTION_BUILTIN_EXT] is True
    assert custom_rec["error"]["type"] == "MyError"
    assert codec.EXCEPTION_BUILTIN_EXT not in custom_rec["extensions"]


def test_a_call_that_does_not_bind_raises_and_records_nothing(sink: _ListWriter) -> None:
    with pytest.raises(TypeError):
        add()  # type: ignore[call-arg]
    assert sink.records == []


def test_a_recording_failure_never_reaches_the_workload(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def append_tool_call(self, rec: dict[str, Any]) -> None:
            raise OSError("disk full")

    hook = PythonToolHook(Broken(), "0123456789abcdef")  # type: ignore[arg-type]
    hook.install()
    try:
        assert add(2) == {"sum": 4, "opts": {}}
    finally:
        hook.uninstall()


# ── nesting, through the real CapsuleWriter ─────────────────────────────────


def test_nested_records_are_marked_and_the_boundary_stays_servable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    writer = CapsuleWriter("run-nested", tmp_path)
    writer.open()
    hook = PythonToolHook(writer, "0123456789abcdef")
    hook.install()

    @record.tool
    def outer(x: int) -> dict[str, Any]:
        writer.append_model_call({"model_call_id": "m1"})  # e.g. an SDK hook's record
        return {"inner": add(x)}

    try:
        assert outer(1) == {"inner": {"sum": 3, "opts": {}}}
    finally:
        hook.uninstall()
    tools = [json.loads(x) for x in (writer.capsule_dir / "tool-calls.jsonl").read_text().splitlines()]
    models = [json.loads(x) for x in (writer.capsule_dir / "model-calls.jsonl").read_text().splitlines()]
    inner, outer_rec = tools  # the inner call finishes (and is written) first
    assert inner["tool_name"] == "add" and outer_rec["tool_name"] == "outer"
    assert inner["extensions"][codec.WITHIN_TOOL_CALL_EXT] == outer_rec["tool_call_id"]
    assert models[0]["extensions"][codec.WITHIN_TOOL_CALL_EXT] == outer_rec["tool_call_id"]
    assert outer_rec["extensions"][codec.NESTED_RECORDS_EXT] == 2
    assert codec.WITHIN_TOOL_CALL_EXT not in outer_rec["extensions"]
    # ADR-0306 slice 4: nesting is no longer a reason -- replay serves the boundary
    # and consumes its marked records as covered.
    assert outer_rec["extensions"][codec.RESULT_CODEC_EXT] == codec.CODEC_JSON
    assert codec.NOT_SERVABLE_REASON_EXT not in outer_rec["extensions"]
    assert not_servable_reason(outer_rec) is None
    assert outer_rec["result"] == {"value": {"inner": {"sum": 3, "opts": {}}}}
    assert is_servable_tool_record(inner)
    # Outside any boundary, records are left untouched.
    writer.append_model_call({"model_call_id": "m2"})
    last = json.loads((writer.capsule_dir / "model-calls.jsonl").read_text().splitlines()[-1])
    assert "extensions" not in last


def test_the_hook_is_a_built_in_with_an_always_available_target() -> None:
    assert (
        "novafabric.capture.record",
        "novafabric.capture.hooks._python_tool",
        "PythonToolHook",
    ) in _BUILT_IN_HOOKS


def test_install_all_records_decorated_calls_into_the_capsule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.capture.hooks import install_all, uninstall_all

    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    writer = CapsuleWriter("run-install", tmp_path)
    writer.open()
    token = install_all(writer, "0123456789abcdef")
    try:
        add(5)
    finally:
        uninstall_all(token)
    assert record._get_tool_handler() is None
    lines = (writer.capsule_dir / "tool-calls.jsonl").read_text().splitlines()
    assert [json.loads(x)["tool_name"] for x in lines] == ["add"]


# ── golden fixtures ──────────────────────────────────────────────────────────


def _fixture(name: str) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (FIXTURES / name).read_text().splitlines() if x.strip()]


def test_valid_fixture_records_validate_and_classify() -> None:
    success, error, payloads_off = _fixture("python-function-valid.jsonl")
    for rec in (success, error, payloads_off):
        jsonschema.validate(rec, _TOOL_SCHEMA)
        assert tool_surface(rec) == TOOL_SURFACE_PYTHON
    assert is_servable_tool_record(success) and is_servable_tool_record(error)
    assert codec.decode_result(success) == (True, success["result"]["value"])
    assert not_servable_reason(payloads_off) == PAYLOADS_OFF_REASON


def test_invalid_fixture_records_are_never_served() -> None:
    """Line 1 adds a top-level field (schema-invalid); line 2 lacks the surface
    marker (an older/foreign python record: not intercepted); line 3 claims
    json-v1 with no result; line 4 names a codec replay does not decode."""
    extra_field, unmarked, no_result, foreign_codec = _fixture("python-function-invalid.jsonl")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(extra_field, _TOOL_SCHEMA)
    for rec in (unmarked, no_result, foreign_codec):
        jsonschema.validate(rec, _TOOL_SCHEMA)
        assert not is_servable_tool_record(rec)
    assert tool_surface(unmarked) is None
    assert codec.decode_result(foreign_codec) == (False, None)
