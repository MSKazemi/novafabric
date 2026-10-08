"""Shared fixtures for the mocked-replay end-to-end tests (issues #12 / #16, ADR-0300).

Provides:

* ``AGENT_SOURCE`` -- a small tool-using agent driven by a JSON plan
  (``AGENT_PLAN``). It talks to the **real** ``openai`` SDK through an
  ``httpx.MockTransport`` that serves ``AGENT_CANNED`` responses during capture
  and raises if the network is reached otherwise, and to a **real** in-memory
  MCP server (``FastMCP`` + ``create_connected_server_and_client_session``)
  whose tools append to ``AGENT_SIDE`` -- so a tool that executes live is
  visible on disk.
* ``write_fake_anthropic`` -- a minimal ``anthropic`` package (the real SDK is not
  a dependency) whose ``Messages.create`` serves ``FAKE_ANTHROPIC_CANNED`` and
  raises otherwise.
* record builders and a synthetic-capsule writer for scenario tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

AGENT_SOURCE = r'''
import asyncio, json, os, sys
from pathlib import Path

PLAN = json.loads(os.environ["AGENT_PLAN"])
OUT = Path(os.environ["AGENT_OUT"])
SIDE = Path(os.environ["AGENT_SIDE"])
CANNED = json.loads(os.environ.get("AGENT_CANNED") or "null")
_served = [0]
obs = []


def _transport(request):
    if CANNED is None:
        raise RuntimeError("network reached during replay")
    body = CANNED[_served[0]]
    _served[0] += 1
    import httpx
    return httpx.Response(200, json=body)


def _openai_client():
    import httpx, openai
    return openai.OpenAI(api_key="sk-test",
                         http_client=httpx.Client(transport=httpx.MockTransport(_transport)))


def _async_openai_client():
    import httpx, openai
    async def handler(request):
        return _transport(request)
    return openai.AsyncOpenAI(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _side(line):
    with SIDE.open("a") as fh:
        fh.write(line + "\n")


async def _run_step(step, session):
    op = step["op"]
    if op == "chat":
        r = _openai_client().chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
        msg = r.choices[0].message
        return {"op": op, "content": msg.content, "finish": r.choices[0].finish_reason,
                "tool_calls": [{"id": tc.id, "name": tc.function.name,
                                "arguments": json.loads(tc.function.arguments)}
                               for tc in (msg.tool_calls or [])]}
    if op == "anthropic":
        import anthropic
        r = anthropic.Anthropic().messages.create(
            model="claude-x", max_tokens=16, messages=[{"role": "user", "content": "hi"}])
        return {"op": op, "stop_reason": r.stop_reason,
                "blocks": [{"type": b.type, "text": getattr(b, "text", None),
                            "id": getattr(b, "id", None), "name": getattr(b, "name", None),
                            "input": getattr(b, "input", None)} for b in r.content]}
    if op == "tool":
        res = await session.call_tool(step["name"], step["args"])
        return {"op": op, "text": res.content[0].text if res.content else None,
                "isError": bool(res.isError)}
    if op == "async_chat":
        r = await _async_openai_client().chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
        return {"op": op, "content": r.choices[0].message.content}
    if op == "stream_chat":
        _openai_client().chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True)
        return {"op": op}
    if op == "responses":
        _openai_client().responses.create(model="gpt-4o", input="hi")
        return {"op": op}
    raise ValueError(op)


async def main():
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session

    server = FastMCP("replay-fixture")

    @server.tool()
    def get_weather(city: str) -> str:
        _side(f"get_weather {city}")
        return f"{city}: live"

    @server.tool()
    def write_file(path: str, text: str) -> str:
        _side(f"write_file {path}")
        return "written"

    @server.tool()
    def fetch_url(url: str) -> str:
        _side(f"fetch_url {url}")
        return "fetched"

    async with create_connected_server_and_client_session(server) as session:
        for step in PLAN:
            try:
                obs.append(await _run_step(step, session))
            except Exception as exc:
                obs.append({"op": step["op"], "error": type(exc).__name__, "message": str(exc)})
                if not step.get("catch"):
                    OUT.write_text(json.dumps(obs))
                    sys.exit(3)
    OUT.write_text(json.dumps(obs))


asyncio.run(main())
'''

_FAKE_ANTHROPIC_INIT = """
from anthropic.resources.messages import AsyncMessages, Messages


class Anthropic:
    def __init__(self, **kwargs):
        self.messages = Messages()


class AsyncAnthropic:
    def __init__(self, **kwargs):
        self.messages = AsyncMessages()
"""

_FAKE_ANTHROPIC_MESSAGES = """
import json, os, types

_served = [0]


def _canned():
    raw = os.environ.get("FAKE_ANTHROPIC_CANNED")
    if not raw:
        raise RuntimeError("network reached during replay (fake anthropic)")
    item = json.loads(raw)[_served[0]]
    _served[0] += 1
    return types.SimpleNamespace(
        id=item["id"], model=item["model"], stop_reason=item["stop_reason"],
        usage=types.SimpleNamespace(input_tokens=3, output_tokens=5),
        content=[types.SimpleNamespace(**block) for block in item["content"]],
    )


class Messages:
    def create(self, **kwargs):
        return _canned()

    def stream(self, **kwargs):
        raise RuntimeError("network reached during replay (fake anthropic stream)")


class AsyncMessages:
    async def create(self, **kwargs):
        return _canned()
"""


def write_fake_anthropic(root: Path) -> Path:
    """Write the fake ``anthropic`` package under ``root``; return ``root``."""
    pkg = root / "anthropic"
    (pkg / "resources").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(_FAKE_ANTHROPIC_INIT)
    (pkg / "resources" / "__init__.py").write_text("")
    (pkg / "resources" / "messages.py").write_text(_FAKE_ANTHROPIC_MESSAGES)
    return root


def write_agent(root: Path) -> Path:
    path = root / "agent.py"
    path.write_text(AGENT_SOURCE)
    return path


# ── canned provider responses ────────────────────────────────────────────────


def openai_body(
    *, content: str | None = None, tool_calls: list[tuple[str, str, dict[str, Any]]] = (),
    rid: str = "chatcmpl-1",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, n, a in tool_calls
        ]
    return {
        "id": rid, "object": "chat.completion", "created": 1, "model": "gpt-4o",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


# ── synthetic capsule records ────────────────────────────────────────────────

_RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def openai_record(
    content: str | None = "ok",
    tool_calls: list[dict[str, Any]] | None = None,
    *, system: str = "openai", call_id: str = "01HXAY7M5JZ8R7K4P9DPBYK2M0",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "model_call_id": call_id,
        "gen_ai.system": system,
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.response.model": "gpt-4o",
        "gen_ai.response.choices": [{
            "index": 0, "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "status": "success",
    }


def mcp_record(
    name: str, arguments: dict[str, Any], text: str, *,
    tool_call_id: str = "01HXAY7M5JZ8R7K4P9DPBYK2T0",
    mutation_class: str = "unknown", status: str = "success",
    started_at: str = "2026-10-08T00:00:00.000000Z",
    finished_at: str = "2026-10-08T00:00:01.000000Z",
) -> dict[str, Any]:
    result = {"content": [{"type": "text", "text": text}], "isError": False}
    return {
        "schema_version": "0.1.0",
        "tool_call_id": tool_call_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "tool_name": name,
        "transport": "mcp",
        "mutation_class": mutation_class,
        "mutates": mutation_class not in ("none", "read-only", "unknown"),
        "arguments": arguments,
        "result": result,
        "status": status,
        "mcp": {
            "method": "tools/call",
            "response_envelope": {"jsonrpc": "2.0", "id": "1", "result": result},
        },
    }


def write_capsule(
    root: Path,
    agent: Path,
    model_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]] = (),  # type: ignore[assignment]
    command: list[str] | None = None,
) -> Path:
    cap = root / "capsule"
    cap.mkdir(parents=True, exist_ok=True)
    (cap / "capsule.yaml").write_text(json.dumps({
        "schema_version": "0.1.0",
        "run_id": _RUN_ID,
        "command": command or [sys.executable, str(agent)],
    }))
    (cap / "model-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in model_calls))
    (cap / "tool-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in tool_calls))
    return cap
