"""ADR-0306 slice 1: ``record.tool`` round-trips through real ``nova capture`` and
mocked ``nova replay`` subprocesses.

Each test captures a real Python agent whose decorated tools append to a
side-effect log, then replays the capsule. A served call never runs its body, so
the side-effect log stays empty during replay; every fail-closed case is proved
the same way -- the refusal happens *before* the body runs.

Acceptance criteria (ADR-0306, slice 1) covered here: AC 3-10.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

REPO = Path(__file__).resolve().parents[2]
_RESULT_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "replay-result.schema.json").read_text()
)
_TOOL_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "tool-call.schema.json").read_text()
)

#: Never appears in source the replay reads; checked for in the event log/result.
SENTINEL = "SENTINEL-7f3a9c"

AGENT = '''
import asyncio, inspect, json, os, sys
from novafabric.capture import record

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]


def side(text):
    with open(SIDE, "a") as fh:
        fh.write(text + "\\n")


class Ctx:
    pass


class MyError(Exception):
    pass


@record.tool(mutation_class="external-side-effect")
def add(a: int, b: int = 2) -> dict:
    side(f"add {a} {b}")
    return {"sum": a + b, "tag": os.environ.get("AGENT_TAG", "")}


@record.tool(mutation_class="none")
def pure(x):
    side(f"pure {x}")
    return {"x": x, "live": os.environ.get("AGENT_TAG", "")}


@record.tool
def unknown_tool(x):
    side(f"unknown {x}")
    return {"x": x}


@record.tool
async def look(order_id: str) -> list:
    side(f"look {order_id}")
    await asyncio.sleep(0)
    return [order_id, {"status": "shipped"}]


@record.tool
def tup():
    side("tup")
    return (1, 2)


@record.tool
def obj():
    side("obj")
    return Ctx()


@record.tool
def big(n):
    side("big")
    return "x" * n


@record.tool
def with_ctx(ctx, q):
    side(f"with_ctx {q}")
    return {"q": q}


@record.tool(ignore=("ctx",))
def ctx_ignored(ctx, q):
    side(f"ctx_ignored {q}")
    return {"q": q}


@record.tool
def boom(kind):
    side(f"boom {kind}")
    if kind == "value":
        raise ValueError("bad value 42")
    raise MyError("custom failure")


@record.tool
def outer(x):
    side(f"outer {x}")
    return {"inner": add(x)}


@record.tool
def secret():
    side("secret")
    return {"key": "AKIA" + "QZ7XK2M4PZT3W6RN"}


@record.tool
def sentinel(arg):
    side("sentinel")
    return {"echo": arg}


@record.tool
def login(user, token):
    side("login")
    return {"user": user, "token_seen": token}


TOOLS = {f.__name__: f for f in (
    add, pure, unknown_tool, look, tup, obj, big, with_ctx, ctx_ignored, boom, outer,
    secret, sentinel, login,
)}


def run(op):
    fn = TOOLS[op["tool"]]
    args = [Ctx() if a == "<ctx>" else a for a in op.get("args", [])]
    kwargs = op.get("kwargs", {})
    if inspect.iscoroutinefunction(fn):
        return asyncio.run(fn(*args, **kwargs))
    return fn(*args, **kwargs)


out = []
for op in json.loads(os.environ["AGENT_PLAN"]):
    try:
        value = run(op)
        out.append({"value": value if not isinstance(value, (tuple, Ctx)) else repr(type(value))})
    except BaseException as exc:  # the workload swallows every failure
        out.append({"error": type(exc).__name__, "message": str(exc)})
with open(OUT, "w") as fh:
    json.dump(out, fh)
'''


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        self.side = root / "side.log"
        self.out = root / "out.json"
        self.runs = root / "runs"
        self.replays = root / "replays"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_SIDE", str(self.side))
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
        for var in ("FORCE_COLOR", "COLORTERM", "AGENT_TAG", "NOVAFABRIC_TOOL_RESULT_MAX_BYTES"):
            monkeypatch.delenv(var, raising=False)

    def capture(self, plan: list[dict[str, Any]]) -> tuple[Path, list[dict[str, Any]]]:
        self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        self._mp.setenv("AGENT_TAG", "captured")
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        captured = json.loads(self.out.read_text())
        self.out.unlink()
        if self.side.exists():
            self.side.unlink()
        self._mp.setenv("AGENT_TAG", "live-in-replay")
        return capsule, captured

    def records(self, capsule: Path) -> list[dict[str, Any]]:
        lines = (capsule / "tool-calls.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def write_records(self, capsule: Path, records: list[dict[str, Any]]) -> None:
        (capsule / "tool-calls.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )

    def replay(
        self, capsule: Path, *flags: str, plan: list[dict[str, Any]] | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
        if plan is not None:
            self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        before = set(self.replays.iterdir()) if self.replays.exists() else set()
        runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays), *flags])
        (replay_dir,) = set(self.replays.iterdir()) - before
        result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
        jsonschema.validate(result, _RESULT_SCHEMA)
        observed = json.loads(self.out.read_text()) if self.out.exists() else []
        if self.out.exists():
            self.out.unlink()
        return result, observed, replay_dir

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def _kinds(result: dict[str, Any]) -> list[str]:
    return [d["kind"] for d in result["replay_contract"]["divergences"]]


def _call(tool: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"tool": tool, "args": list(args), "kwargs": kwargs}


# ── AC 3: served round trip ──────────────────────────────────────────────────


def test_decorated_calls_are_served_and_their_bodies_never_run(env: Env) -> None:
    plan = [_call("add", 1), _call("look", "o-1"), _call("pure", 3)]
    capsule, captured = env.capture(plan)
    records = env.records(capsule)
    assert len(records) == 3
    for rec in records:
        jsonschema.validate(rec, _TOOL_SCHEMA)
        assert rec["transport"] == "python"
        assert rec["extensions"]["io.novafabric.result_codec"] == "json-v1"

    result, replayed, _ = env.replay(capsule)
    assert replayed == captured  # the recorded values, not "live-in-replay"
    assert captured[0]["value"]["tag"] == "captured"
    assert env.side_effects == [], "a decorated body ran during mocked replay"
    assert result["status"] == "success", result
    assert result["tool_calls_mocked"] == result["tool_calls_available"] == 3
    assert result["queues_fully_consumed"] is True
    surfaces = result["replay_contract"]["tool_calls_by_surface"]
    assert surfaces["novafabric.capture.record.tool"]["mocked"] == 3
    assert "novafabric.capture.record.tool" in result["replay_contract"]["interception_surfaces"]


# ── AC 4: one-to-one, canonical binding ──────────────────────────────────────


def test_positional_keyword_and_default_calls_match_and_consume_in_order(env: Env) -> None:
    capsule, captured = env.capture([_call("add", 1), _call("add", 1), _call("add", 7)])
    # Replay the same three calls written three different ways.
    plan = [_call("add", a=1, b=2), _call("add", 1, 2), _call("add", 7, b=2)]
    result, replayed, _ = env.replay(capsule, plan=plan)
    assert replayed == captured
    assert env.side_effects == []
    assert result["status"] == "success"
    assert result["tool_calls_mocked"] == 3


# ── AC 5: fail closed ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("plan", "expected_kind"),
    [
        ([_call("add", 1), _call("pure", 9)], "tool_call_unmatched"),  # no record
        ([_call("add", 2)], "tool_call_unmatched"),  # different arguments
        ([_call("add", 1), _call("add", 1)], "tool_call_unmatched"),  # exhausted
    ],
    ids=["no-record", "different-arguments", "exhausted"],
)
def test_an_unmatched_call_fails_closed_before_the_body_runs(
    env: Env, plan: list[dict[str, Any]], expected_kind: str
) -> None:
    capsule, _ = env.capture([_call("add", 1)])
    result, replayed, _ = env.replay(capsule, plan=plan)
    assert env.side_effects == [], "the live body ran for an unmatched call"
    assert expected_kind in _kinds(result)
    assert result["status"] == "failure"
    # The workload swallowed the exception and exited 0 -- still a failure.
    assert result["exit_code"] == 0
    assert result["error"]["type"] == "ReplayDivergence"
    assert any(o.get("error") == "ReplayToolUnmatchedError" for o in replayed)
    assert result["tool_calls_unmatched"] >= 1


def test_a_capsule_captured_before_the_decorator_existed_fails_closed(env: Env) -> None:
    """Red check for serving: the same capsule without its python records."""
    capsule, _ = env.capture([_call("add", 1)])
    env.write_records(capsule, [])
    result, _, _ = env.replay(capsule)
    assert env.side_effects == []
    assert result["status"] == "failure"
    assert "tool_call_unmatched" in _kinds(result)


# ── AC 6: --permissive keeps the safety ladder ──────────────────────────────


def test_permissive_runs_an_unmatched_none_call_live(env: Env) -> None:
    capsule, _ = env.capture([_call("add", 1)])
    result, replayed, _ = env.replay(
        capsule, "--permissive", plan=[_call("add", 1), _call("pure", 5)]
    )
    assert env.side_effects == ["pure 5"]
    assert replayed[1]["value"]["live"] == "live-in-replay"
    assert result["tool_calls_live"] == 1
    assert result["status"] == "success"
    assert "tool_call_unmatched" in _kinds(result)


def test_permissive_refuses_an_unmatched_unknown_call_without_the_ladder_flag(
    env: Env,
) -> None:
    capsule, _ = env.capture([_call("add", 1)])
    plan = [_call("add", 1), _call("unknown_tool", 5)]
    result, replayed, _ = env.replay(capsule, "--permissive", plan=plan)
    assert env.side_effects == []
    assert result["tool_calls_live"] == 0
    assert result["replay_contract"]["tool_calls_refused"] == 1
    assert replayed[1]["error"] == "ReplayToolUnmatchedError"
    assert "--allow-unknown-mutation" in replayed[1]["message"]

    result, _, _ = env.replay(capsule, "--permissive", "--allow-unknown-mutation", plan=plan)
    assert env.side_effects == ["unknown 5"]
    assert result["tool_calls_live"] == 1


# ── AC 7: not servable ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("op", "reason_fragment"),
    [
        (_call("tup"), "tuple"),
        (_call("obj"), "is a Ctx"),
        (_call("big", 64), "over the 16-byte cap"),
        (_call("with_ctx", "<ctx>", "q"), "argument `ctx`"),
        (_call("outer", 4), "model/tool record(s) were written inside this boundary"),
    ],
    ids=["tuple", "object", "size-cap", "non-canonical-argument", "nested"],
)
def test_an_unservable_record_fails_closed_naming_the_cause(
    env: Env, monkeypatch: pytest.MonkeyPatch, op: dict[str, Any], reason_fragment: str
) -> None:
    if op["tool"] == "big":
        monkeypatch.setenv("NOVAFABRIC_TOOL_RESULT_MAX_BYTES", "16")
    capsule, _ = env.capture([op])
    result, replayed, _ = env.replay(capsule)
    assert env.side_effects == [], "the body ran for a call whose record is not servable"
    assert result["status"] == "failure"
    divergences = [
        d for d in result["replay_contract"]["divergences"]
        if d["kind"] == "tool_result_not_servable"
    ]
    assert divergences, _kinds(result)
    assert reason_fragment in divergences[0]["reason"], divergences[0]["reason"]
    assert replayed[0]["error"] == "ReplayToolRecordNotServableError"
    # A tool divergence, never counted as a model one.
    assert result["model_calls_unmatched"] == 0
    assert result["tool_calls_unmatched"] >= 1


def test_ignore_makes_a_non_json_argument_servable(env: Env) -> None:
    capsule, captured = env.capture([_call("ctx_ignored", "<ctx>", "q")])
    (rec,) = env.records(capsule)
    assert rec["arguments"] == {"q": "q"}
    assert rec["extensions"]["io.novafabric.ignored_arguments"] == ["ctx"]
    result, replayed, _ = env.replay(capsule)
    assert replayed == captured
    assert result["status"] == "success"


def test_payloads_off_records_digests_only_and_replay_asks_for_a_recapture(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "standard")
    capsule, _ = env.capture([_call("sentinel", SENTINEL)])
    (rec,) = env.records(capsule)
    assert SENTINEL not in json.dumps(rec)
    assert rec["result"] is None and rec["arguments"] == {}
    assert rec["extensions"]["io.novafabric.result_codec"] == "not-servable"
    result, _, _ = env.replay(capsule)
    assert env.side_effects == []
    assert result["status"] == "failure"
    (div,) = [
        d for d in result["replay_contract"]["divergences"]
        if d["kind"] == "tool_result_not_servable"
    ]
    assert "NOVA_CAPTURE_LEVEL=forensic" in div["reason"]


# ── AC 8: recorded exceptions ───────────────────────────────────────────────


def test_recorded_exceptions_are_replayed_safely(env: Env) -> None:
    plan = [_call("boom", "value"), _call("boom", "custom")]
    capsule, captured = env.capture(plan)
    assert captured[0] == {"error": "ValueError", "message": "bad value 42"}
    result, replayed, _ = env.replay(capsule)
    assert env.side_effects == []
    assert replayed[0] == {"error": "ValueError", "message": "bad value 42"}
    assert replayed[1]["error"] == "ReplayRecordedToolError"
    assert "MyError" in replayed[1]["message"]
    assert result["status"] == "success"  # a faithful replay of recorded failures

    # A capsule that names SystemExit is never allowed to raise it.
    records = env.records(capsule)
    records[0]["error"]["type"] = "SystemExit"
    env.write_records(capsule, records)
    result, replayed, _ = env.replay(capsule)
    assert replayed[0]["error"] == "ReplayRecordedToolError"
    assert "SystemExit" in replayed[0]["message"]


# ── AC 9: surface isolation ──────────────────────────────────────────────────


def test_a_python_record_never_answers_an_mcp_call_and_the_reverse() -> None:
    from novafabric.replay._contract import (
        TOOL_SURFACE_MCP,
        TOOL_SURFACE_PYTHON,
        ReplayEventLog,
        tool_surface,
    )
    from novafabric.replay._dispatcher import MockToolDispatcher

    python_rec = json.loads(
        (REPO / "tests/fixtures/tool-calls/python-function-valid.jsonl").read_text().splitlines()[0]
    )
    mcp_rec = {
        "tool_call_id": "01HXAY7M5JZ8R7K4P9DPBYK2T1", "tool_name": python_rec["tool_name"],
        "transport": "mcp", "arguments": python_rec["arguments"],
        "mcp": {"method": "tools/call"}, "status": "success", "result": {"content": []},
    }
    assert tool_surface(python_rec) == TOOL_SURFACE_PYTHON
    assert tool_surface(mcp_rec) == TOOL_SURFACE_MCP

    only_python = MockToolDispatcher([python_rec], events=ReplayEventLog(None))
    assert only_python.lookup(None, python_rec["tool_name"], python_rec["arguments"]) is None
    both = MockToolDispatcher([python_rec, mcp_rec], events=ReplayEventLog(None))
    assert both.lookup(None, python_rec["tool_name"], python_rec["arguments"]) is mcp_rec


def test_an_mcp_record_never_answers_a_decorated_call(env: Env) -> None:
    capsule, _ = env.capture([_call("add", 1)])
    (rec,) = env.records(capsule)
    mcp_twin = {
        **{k: v for k, v in rec.items() if k != "extensions"},
        "transport": "mcp",
        "mcp": {"method": "tools/call", "server_name": "x"},
        "result": {"content": [{"type": "text", "text": "mcp"}]},
    }
    env.write_records(capsule, [mcp_twin])
    result, _, _ = env.replay(capsule)
    assert env.side_effects == []
    assert result["status"] == "failure"
    assert "tool_call_unmatched" in _kinds(result)


# ── AC 10: security ──────────────────────────────────────────────────────────


def test_no_argument_or_result_value_reaches_the_event_log_or_result(env: Env) -> None:
    capsule, _ = env.capture([_call("sentinel", SENTINEL)])
    # Diverge on purpose so the refusal path writes its event too.
    result, _, replay_dir = env.replay(
        capsule, plan=[_call("sentinel", SENTINEL), _call("sentinel", SENTINEL)]
    )
    assert result["status"] == "failure"
    for path in replay_dir.rglob("*"):
        if path.is_file():
            assert SENTINEL not in path.read_text(errors="replace"), path


def test_a_secret_in_a_tool_result_is_stored_and_served_redacted(env: Env) -> None:
    capsule, captured = env.capture([_call("secret")])
    raw = (capsule / "tool-calls.jsonl").read_text()
    assert "QZ7XK2M4PZT3W6RN" not in raw
    assert "[REDACTED" in raw
    result, replayed, _ = env.replay(capsule)
    assert result["status"] == "success"
    assert "QZ7XK2M4PZT3W6RN" not in json.dumps(replayed)
    assert "[REDACTED" in replayed[0]["value"]["key"]
    assert captured[0]["value"]["key"].startswith("AKIA")  # the live run saw the real value


# ── digests are never derived from a detected secret (NF-166 lesson) ────────

AWS_KEY = "AKIA" + "QZ7XK2M4PZT3W6RN"
GITHUB_TOKEN = "ghp" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp"


def _raw_derived_digests(token: str) -> dict[str, str]:
    """Every digest form this feature writes, computed over the RAW values."""
    import hashlib

    from novafabric.replay._contract import normalized_arg_hash

    raw_args = {"user": "alice", "token": token}
    raw_result = {"user": "alice", "token_seen": token}

    def canon(v: Any) -> str:
        return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    return {
        "arguments digest": normalized_arg_hash(raw_args),
        "result digest": hashlib.sha256(canon(raw_result).encode()).hexdigest(),
        "sha256(secret)": hashlib.sha256(token.encode()).hexdigest(),
        "sha256(arguments json)": hashlib.sha256(canon(raw_args).encode()).hexdigest(),
    }


def _capsule_text(capsule: Path) -> str:
    return "\n".join(
        p.read_text(errors="replace") for p in sorted(capsule.rglob("*")) if p.is_file()
    )


@pytest.mark.parametrize("token", [AWS_KEY, GITHUB_TOKEN], ids=["aws-key", "github-token"])
def test_payloads_off_digests_carry_no_trace_of_a_detected_secret_and_still_match(
    env: Env, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    from novafabric.capture._tool_codec import redacted_arguments_digest

    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "standard")
    capsule, _ = env.capture([_call("login", "alice", token)])
    text = _capsule_text(capsule)
    assert token not in text
    for label, digest in _raw_derived_digests(token).items():
        assert digest not in text, f"the capsule holds a {label} derived from the raw secret"
    # Non-vacuity: the record does carry the digest of the REDACTED arguments.
    (rec,) = env.records(capsule)
    assert rec["extensions"]["io.novafabric.arguments_digest"] == redacted_arguments_digest(
        {"user": "alice", "token": token}
    )

    # Replay redacts the live arguments the same way, so the call still matches its
    # record: the refusal is "not servable (payloads off)", never "unmatched".
    result, _, _ = env.replay(capsule)
    assert env.side_effects == []
    kinds = _kinds(result)
    assert "tool_result_not_servable" in kinds and "tool_call_unmatched" not in kinds
    (div,) = [
        d for d in result["replay_contract"]["divergences"]
        if d["kind"] == "tool_result_not_servable"
    ]
    assert div["consumed"] is True and "NOVA_CAPTURE_LEVEL=forensic" in div["reason"]


def test_a_secret_argument_redacted_in_the_capsule_still_matches_at_forensic_level(
    env: Env,
) -> None:
    capsule, _ = env.capture([_call("login", "alice", GITHUB_TOKEN)])
    (rec,) = env.records(capsule)
    assert GITHUB_TOKEN not in json.dumps(rec)
    assert "[REDACTED:" in rec["arguments"]["token"]
    result, replayed, _ = env.replay(capsule)
    assert env.side_effects == []
    assert result["status"] == "success", _kinds(result)
    assert GITHUB_TOKEN not in json.dumps(replayed)
