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
  a dependency) whose ``Messages.create`` / ``AsyncMessages.create`` (streamed or
  not) serve ``FAKE_ANTHROPIC_CANNED`` and raise otherwise.
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
#: The items a streamed call yielded during the current step, so a step that
#: raised part-way through a stream can report what had been delivered.
_DELIVERED = []


def _transport(request):
    if CANNED is None:
        raise RuntimeError("network reached during replay")
    body = CANNED[_served[0]]
    _served[0] += 1
    import httpx
    if isinstance(body, dict) and "__status__" in body:
        # A failed HTTP attempt (rate limit, 4xx, 5xx), as the provider sends it.
        return httpx.Response(body["__status__"], json=body.get("json"),
                              headers=body.get("headers") or {})
    if isinstance(body, dict) and body.get("__raise__") == "timeout":
        raise httpx.ReadTimeout("read timed out", request=request)
    if isinstance(body, dict) and body.get("__raise__") == "connect":
        raise httpx.ConnectError("connection refused", request=request)
    if isinstance(body, dict) and "__sse__" in body:
        # A streamed response: server-sent events, as the provider sends them.
        lines = []
        for event in body["__sse__"]:
            if body.get("named_events"):
                lines.append("event: " + event["type"])
            lines.append("data: " + json.dumps(event))
            lines.append("")
        if body.get("done", True):
            lines += ["data: [DONE]", ""]
        content = ("\n".join(lines) + "\n").encode()
        if body.get("drop"):
            # The connection drops after these events (no terminal event).
            return httpx.Response(200, stream=_Dropping(content, body["drop"]),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, content=content,
                              headers={"content-type": "text/event-stream"})
    return httpx.Response(200, json=body)


import httpx as _httpx


class _Dropping(_httpx.SyncByteStream, _httpx.AsyncByteStream):
    """A response body that delivers ``data`` and then fails mid-read."""

    def __init__(self, data, how):
        self._data, self._how = data, how

    def _fail(self):
        if self._how == "timeout":
            return _httpx.ReadTimeout("read timed out mid-stream")
        return _httpx.ReadError("connection reset mid-stream")

    def __iter__(self):
        yield self._data
        raise self._fail()

    async def __aiter__(self):
        yield self._data
        raise self._fail()


def _track(items):
    for item in items:
        _DELIVERED.append(item)
        yield item


async def _atrack(items):
    async for item in items:
        _DELIVERED.append(item)
        yield item


def _delivered_obs():
    """What a consumer had folded from a stream that then raised."""
    items = list(_DELIVERED)
    if not items:
        return None
    if hasattr(items[0], "choices"):
        fold = _ChunkFold()
        for item in items:
            fold.add(item)
        return fold.obs("delivered")
    if str(getattr(items[0], "type", "")).startswith("response."):
        kinds = []
        for e in items:
            if e.type not in kinds:
                kinds.append(e.type)
        return {"types": kinds, "text": "".join(
            e.delta for e in items if e.type == "response.output_text.delta")}
    fold = _EventFold()
    for item in items:
        fold.add(item)
    return fold.obs("delivered")


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


def _sdk_error_obs(exc):
    """What an ``except`` block reads off an SDK error (only when the SDK set it)."""
    out = {}
    for name in ("status_code", "body", "request_id", "code", "type", "param"):
        value = getattr(exc, name, None)
        if value is not None and not callable(value):
            out[name] = value
    response = getattr(exc, "response", None)
    if response is not None:
        out["retry_after_ms"] = response.headers.get("retry-after-ms")
        out["response_status"] = response.status_code
        try:
            out["response_json"] = response.json()
        except Exception:
            out["response_json"] = None
    request = getattr(exc, "request", None)
    if request is not None:
        out["request"] = [request.method, str(request.url)]
    if out:
        out["mro"] = [c.__name__ for c in type(exc).__mro__]
    return out


def _side(line):
    with SIDE.open("a") as fh:
        fh.write(line + "\n")


CHAT = dict(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
MSG = dict(model="claude-x", max_tokens=16, messages=[{"role": "user", "content": "hi"}])


def _chat_obs(op, r):
    msg = r.choices[0].message
    return {"op": op, "content": msg.content, "finish": r.choices[0].finish_reason,
            "tool_calls": [{"id": tc.id, "name": tc.function.name,
                            "arguments": json.loads(tc.function.arguments)}
                           for tc in (msg.tool_calls or [])]}


class _ChunkFold:
    """What a streaming consumer reconstructs from chat chunks."""

    def __init__(self):
        self.content, self.calls, self.finish, self.usage = [], {}, None, None

    def add(self, chunk):
        if getattr(chunk, "usage", None):
            self.usage = chunk.usage.total_tokens
        for c in chunk.choices:
            d = c.delta
            if d.content:
                self.content.append(d.content)
            for tc in d.tool_calls or []:
                slot = self.calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                slot["id"] = tc.id or slot["id"]
                if tc.function is not None:
                    slot["name"] = tc.function.name or slot["name"]
                    slot["arguments"] += tc.function.arguments or ""
            if c.finish_reason:
                self.finish = c.finish_reason

    def obs(self, op):
        return {"op": op, "content": "".join(self.content) or None, "finish": self.finish,
                "usage": self.usage,
                "tool_calls": [{"id": v["id"], "name": v["name"],
                                "arguments": json.loads(v["arguments"] or "{}")}
                               for _, v in sorted(self.calls.items())]}


def _responses_obs(op, r):
    return {"op": op, "text": r.output_text, "status": r.status,
            "response_error": r.error.model_dump(exclude_unset=True) if r.error else None,
            "incomplete": r.incomplete_details.reason if r.incomplete_details else None,
            "calls": [{"call_id": i.call_id, "name": i.name,
                       "arguments": json.loads(i.arguments)}
                      for i in r.output if i.type == "function_call"]}


def _responses_stream_obs(op, events):
    deltas, final = [], None
    for e in events:
        if e.type == "response.output_text.delta":
            deltas.append(e.delta)
        elif e.type in ("response.completed", "response.incomplete", "response.failed"):
            final = e.response
    return {**_responses_obs(op, final), "delta_text": "".join(deltas)}


def _anthropic_obs(op, r):
    return {"op": op, "stop_reason": r.stop_reason,
            "blocks": [{"type": b.type, "text": getattr(b, "text", None),
                        "id": getattr(b, "id", None), "name": getattr(b, "name", None),
                        "input": getattr(b, "input", None)} for b in r.content]}


class _EventFold:
    """What a streaming consumer reconstructs from Anthropic raw events."""

    def __init__(self):
        self.blocks, self.stop_reason = {}, None

    def add(self, e):
        if e.type == "content_block_start":
            cb = e.content_block
            self.blocks[e.index] = {"type": cb.type, "text": "", "id": getattr(cb, "id", None),
                                    "name": getattr(cb, "name", None), "json": ""}
        elif e.type == "content_block_delta":
            if e.delta.type == "text_delta":
                self.blocks[e.index]["text"] += e.delta.text
            else:
                self.blocks[e.index]["json"] += e.delta.partial_json
        elif e.type == "message_delta":
            self.stop_reason = e.delta.stop_reason

    def obs(self, op):
        blocks = []
        for _, b in sorted(self.blocks.items()):
            if b["type"] == "tool_use":
                blocks.append({"type": "tool_use", "text": None, "id": b["id"],
                               "name": b["name"], "input": json.loads(b["json"] or "{}")})
            else:
                blocks.append({"type": "text", "text": b["text"], "id": None, "name": None,
                               "input": None})
        return {"op": op, "stop_reason": self.stop_reason, "blocks": blocks}


async def _run_step(step, session):
    op = step["op"]
    if op == "chat":
        return _chat_obs(op, _openai_client().chat.completions.create(**CHAT))
    if op == "anthropic":
        import anthropic
        return _anthropic_obs(op, anthropic.Anthropic().messages.create(**MSG))
    if op == "anthropic_async":
        import anthropic
        return _anthropic_obs(op, await anthropic.AsyncAnthropic().messages.create(**MSG))
    if op == "anthropic_stream":
        import anthropic
        fold = _EventFold()
        for e in _track(anthropic.Anthropic().messages.create(stream=True, **MSG)):
            fold.add(e)
        return fold.obs(op)
    if op == "anthropic_async_stream":
        import anthropic
        fold = _EventFold()
        async for e in _atrack(
                await anthropic.AsyncAnthropic().messages.create(stream=True, **MSG)):
            fold.add(e)
        return fold.obs(op)
    if op == "anthropic_stream_helper":
        import anthropic
        anthropic.Anthropic().messages.stream(**MSG)
        return {"op": op}
    if op == "tool":
        res = await session.call_tool(step["name"], step["args"])
        return {"op": op, "text": res.content[0].text if res.content else None,
                "isError": bool(res.isError)}
    if op == "async_chat":
        return _chat_obs(op, await _async_openai_client().chat.completions.create(**CHAT))
    if op == "stream_chat":
        fold = _ChunkFold()
        extra = {"stream_options": {"include_usage": True}} if step.get("usage") else {}
        with _openai_client().chat.completions.create(stream=True, **CHAT, **extra) as s:
            for chunk in _track(s):
                fold.add(chunk)
        return fold.obs(op)
    if op == "stream_chat_partial":
        # Read only the first chunk, then abandon the stream.
        s = _openai_client().chat.completions.create(stream=True, **CHAT)
        first = next(iter(s))
        s.close()
        return {"op": op, "first": first.choices[0].delta.content}
    if op == "async_stream_chat":
        fold = _ChunkFold()
        async for chunk in _atrack(await _async_openai_client().chat.completions.create(
                stream=True, **CHAT)):
            fold.add(chunk)
        return fold.obs(op)
    if op == "chat_stream_helper":
        with _openai_client().chat.completions.stream(**CHAT) as s:
            for _ in s:
                pass
            return _chat_obs(op, s.get_final_completion())
    if op == "responses":
        return _responses_obs(op, _openai_client().responses.create(model="gpt-4o", input="hi"))
    if op == "async_responses":
        return _responses_obs(
            op, await _async_openai_client().responses.create(model="gpt-4o", input="hi"))
    if op == "stream_responses":
        return _responses_stream_obs(
            op, list(_track(_openai_client().responses.create(
                model="gpt-4o", input="hi", stream=True))))
    if op == "async_stream_responses":
        stream = await _async_openai_client().responses.create(
            model="gpt-4o", input="hi", stream=True)
        return _responses_stream_obs(op, [e async for e in _atrack(stream)])
    if op == "responses_stream_helper":
        with _openai_client().responses.stream(model="gpt-4o", input="hi") as s:
            for _ in s:
                pass
            return _responses_obs(op, s.get_final_response())
    if op == "net":
        # A "tool" that is plain network I/O: replay cannot intercept it, only see it.
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        socket.create_connection(("127.0.0.1", srv.getsockname()[1])).close()
        srv.close()
        return {"op": op}
    if op == "parse":
        _openai_client().chat.completions.parse(**CHAT)
        return {"op": op}
    if op == "legacy":
        _openai_client().completions.create(model="gpt-3.5-turbo-instruct", prompt="hi")
        return {"op": op}
    if op == "raw":
        _openai_client().chat.completions.with_raw_response.create(**CHAT)
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
            _DELIVERED.clear()
            try:
                obs.append(await _run_step(step, session))
            except Exception as exc:
                delivered = _delivered_obs()
                obs.append({"op": step["op"], "error": type(exc).__name__, "message": str(exc),
                            **_sdk_error_obs(exc),
                            **({"delivered": delivered} if delivered else {})})
                if not step.get("catch"):
                    OUT.write_text(json.dumps(obs))
                    sys.exit(3)
    OUT.write_text(json.dumps(obs))


asyncio.run(main())
'''

_FAKE_ANTHROPIC_INIT = """
from anthropic._exceptions import (
    APIConnectionError, APIError, APIStatusError, APITimeoutError, AnthropicError,
    BadRequestError, InternalServerError, OverloadedError, RateLimitError,
)
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


def _ns(value):
    if isinstance(value, dict):
        return types.SimpleNamespace(**{k: _ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_ns(v) for v in value]
    return value


def _sdk_error(spec):
    import httpx
    from anthropic import _exceptions
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(spec["status"], json=spec.get("body"),
                              headers=spec.get("headers") or {}, request=request)
    cls = getattr(_exceptions, spec["class"])
    return cls(f"Error code: {spec['status']} - {spec.get('body')}",
               response=response, body=spec.get("body"))


def _next_item():
    raw = os.environ.get("FAKE_ANTHROPIC_CANNED")
    if not raw:
        raise RuntimeError("network reached during replay (fake anthropic)")
    item = json.loads(raw)[_served[0]]
    _served[0] += 1
    if "__error__" in item:
        # A canned failure: raised as the SDK would after its own retries.
        raise _sdk_error(item["__error__"])
    return item


def _canned():
    item = _next_item()
    return types.SimpleNamespace(
        id=item["id"], model=item["model"], stop_reason=item["stop_reason"],
        usage=types.SimpleNamespace(input_tokens=3, output_tokens=5),
        content=[types.SimpleNamespace(**block) for block in item["content"]],
    )


def _events():
    # A canned streamed call is {"events": [raw stream events]}; an event
    # {"__error__": spec} is raised when the consumer reaches it (mid-stream).
    events = _next_item()["events"]

    def gen():
        for e in events:
            if "__error__" in e:
                raise _sdk_error(e["__error__"])
            yield _ns(e)

    return gen()


class Messages:
    def create(self, **kwargs):
        if kwargs.get("stream"):
            return _events()
        return _canned()

    def stream(self, **kwargs):
        raise RuntimeError("network reached during replay (fake anthropic stream)")


class AsyncMessages:
    async def create(self, **kwargs):
        if kwargs.get("stream"):
            events = _events()

            async def gen():
                for e in events:
                    yield e

            return gen()
        return _canned()
"""


#: The real SDK's exception hierarchy and constructors (Stainless-generated, the
#: same shape as ``openai._exceptions``), reduced to what replay rebuilds.
_FAKE_ANTHROPIC_EXCEPTIONS = """
class AnthropicError(Exception):
    pass


class APIError(AnthropicError):
    def __init__(self, message, request, *, body):
        super().__init__(message)
        self.request = request
        self.message = message
        self.body = body


class APIStatusError(APIError):
    def __init__(self, message, *, response, body):
        super().__init__(message, response.request, body=body)
        self.response = response
        self.status_code = response.status_code
        self.request_id = response.headers.get("request-id")


class APIConnectionError(APIError):
    def __init__(self, *, message="Connection error.", request):
        super().__init__(message, request, body=None)


class APITimeoutError(APIConnectionError):
    def __init__(self, request):
        super().__init__(message="Request timed out.", request=request)


class BadRequestError(APIStatusError):
    status_code = 400


class RateLimitError(APIStatusError):
    status_code = 429


class InternalServerError(APIStatusError):
    pass


class OverloadedError(APIStatusError):
    status_code = 529
"""


def write_fake_anthropic(root: Path) -> Path:
    """Write the fake ``anthropic`` package under ``root``; return ``root``."""
    pkg = root / "anthropic"
    (pkg / "resources").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(_FAKE_ANTHROPIC_INIT)
    (pkg / "_exceptions.py").write_text(_FAKE_ANTHROPIC_EXCEPTIONS)
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


def openai_chunks(
    *, content: str | None = None, tool_calls: list[tuple[str, str, dict[str, Any]]] = (),
    rid: str = "chatcmpl-s", usage: bool = False, split: int = 3,
) -> dict[str, Any]:
    """A streamed chat completion as the provider sends it: content split into
    several deltas, each tool call's arguments split across two deltas."""
    base = {"id": rid, "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o"}

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    events = [chunk({"role": "assistant", "content": ""})]
    if content:
        step = max(1, len(content) // split)
        events += [chunk({"content": content[i:i + step]})
                   for i in range(0, len(content), step)]
    for n, (call_id, name, args) in enumerate(tool_calls):
        text = json.dumps(args)
        half = len(text) // 2
        events.append(chunk({"tool_calls": [{"index": n, "id": call_id, "type": "function",
                                             "function": {"name": name,
                                                          "arguments": text[:half]}}]}))
        events.append(chunk({"tool_calls": [{"index": n,
                                             "function": {"arguments": text[half:]}}]}))
    events.append(chunk({}, "tool_calls" if tool_calls else "stop"))
    if usage:
        events.append({**base, "choices": [], "usage": {
            "prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}})
    return {"__sse__": events}


def responses_body(
    *, text: str | None = None, calls: list[tuple[str, str, dict[str, Any]]] = (),
    rid: str = "resp_1",
) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    if text is not None:
        output.append({"type": "message", "id": "msg_x", "role": "assistant",
                       "status": "completed",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]})
    for call_id, name, args in calls:
        output.append({"type": "function_call", "id": "fc_x" + call_id, "call_id": call_id,
                       "name": name, "arguments": json.dumps(args), "status": "completed"})
    return {
        "id": rid, "object": "response", "created_at": 1, "model": "gpt-4o",
        "status": "completed", "output": output, "parallel_tool_calls": True,
        "tool_choice": "auto", "tools": [], "error": None, "incomplete_details": None,
        "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }


def responses_sse(body: dict[str, Any]) -> dict[str, Any]:
    """A streamed Responses API call, as the provider sends it: created, then
    per message item added/part/text deltas/done, then completed."""
    events: list[dict[str, Any]] = [{
        "type": "response.created", "response": {**body, "status": "in_progress", "output": []},
    }]
    for index, item in enumerate(body["output"]):
        if item["type"] != "message":
            continue
        text = item["content"][0]["text"]
        part = {"type": "output_text", "text": "", "annotations": []}
        ids = {"item_id": item["id"], "output_index": index, "content_index": 0}
        events.append({"type": "response.output_item.added", "output_index": index,
                       "item": {**item, "status": "in_progress", "content": []}})
        events.append({"type": "response.content_part.added", **ids, "part": part})
        events += [{"type": "response.output_text.delta", **ids, "delta": text[i:i + 4],
                    "logprobs": []} for i in range(0, len(text), 4)]
        events.append({"type": "response.output_text.done", **ids, "text": text,
                       "logprobs": []})
        events.append({"type": "response.content_part.done", **ids,
                       "part": {**part, "text": text}})
        events.append({"type": "response.output_item.done", "output_index": index,
                       "item": item})
    terminal = {"failed": "response.failed", "incomplete": "response.incomplete"}
    events.append({"type": terminal.get(body["status"], "response.completed"),
                   "response": body})
    for n, event in enumerate(events):
        event["sequence_number"] = n
    return {"__sse__": events, "named_events": True, "done": False}


def anthropic_events(
    text: str, tool: tuple[str, str, dict[str, Any]] | None = None,
    stop_reason: str = "end_turn", mid: str = "msg_s",
) -> dict[str, Any]:
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": "claude-x",
            "content": [], "stop_reason": None,
            "usage": {"input_tokens": 3, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
    ]
    events += [{"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text[i:i + 3]}}
               for i in range(0, len(text), 3)]
    events.append({"type": "content_block_stop", "index": 0})
    if tool:
        tool_id, name, args = tool
        raw = json.dumps(args)
        events += [
            {"type": "content_block_start", "index": 1, "content_block": {
                "type": "tool_use", "id": tool_id, "name": name, "input": {}}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": raw[:5]}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": raw[5:]}},
            {"type": "content_block_stop", "index": 1},
        ]
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop_reason},
         "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    return {"events": events}


# ── synthetic capsule records ────────────────────────────────────────────────

_RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def openai_record(
    content: str | None = "ok",
    tool_calls: list[dict[str, Any]] | None = None,
    *, system: str = "openai", call_id: str = "01HXAY7M5JZ8R7K4P9DPBYK2M0",
    surface: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    extra: dict[str, Any] = (
        {"extensions": {"io.novafabric.api_surface": surface}} if surface else {}
    )
    return {
        **extra,
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
