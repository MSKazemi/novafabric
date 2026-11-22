"""A workload that talks to an in-process fake Ollama server (ADR-0320 tests).

Shape of probe P7 (design/research/replay-diagnosis-2026-11-19): the agent starts
a stdlib ``http.server`` thread that answers Ollama ``/api/chat`` and
``/api/generate`` (JSON for ``"stream": false``, NDJSON chunks otherwise), points
``OLLAMA_BASE_URL`` at it, and runs the steps in ``AGENT_PLAN``. The server's
answers carry ``OLLAMA_CANNED``, so a capture and a replay run with different
values show who answered; every request the server receives is appended to
``AGENT_SIDE``. What the agent observed goes to ``AGENT_OUT`` (a JSON list).

Plan steps: ``{"op": "chat" | "generate", "stream": bool, "async": bool,
"tools": bool, "abandon": bool, "client": "raw" | "ollama" | "langchain",
"catch": bool}``.
"""

from __future__ import annotations

from pathlib import Path

AGENT_SOURCE = r'''
import asyncio, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CANNED = os.environ.get("OLLAMA_CANNED", "capture-answer")
SIDE = os.environ["AGENT_SIDE"]
COUNT = {"n": 0}


def _answer(path, body):
    COUNT["n"] += 1
    n = COUNT["n"]
    tools = bool(body.get("tools"))
    text = "" if tools else f"{CANNED} #{n}"
    calls = [{"function": {"name": "get_weather", "arguments": {"city": CANNED}}}] if tools else None
    final = {"done": True, "done_reason": "stop", "total_duration": 1000 + n,
             "prompt_eval_count": 7, "eval_count": 3 + n}
    head = {"model": "fake-model", "created_at": f"2026-01-01T00:00:0{n % 10}Z"}
    if body.get("stream") is False:
        if path == "/api/chat":
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            return [{**head, "message": msg, **final}]
        return [{**head, "response": text, **final}]
    halves = [text[: len(text) // 2], text[len(text) // 2:]] if text else []
    out = []
    for part in halves:
        out.append({**head, **({"message": {"role": "assistant", "content": part}}
                               if path == "/api/chat" else {"response": part}), "done": False})
    if calls:
        out.append({**head, "message": {"role": "assistant", "content": "", "tool_calls": calls},
                    "done": False})
    last = {"message": {"role": "assistant", "content": ""}} if path == "/api/chat" else {"response": ""}
    out.append({**head, **last, **final})
    return out


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        with open(SIDE, "a") as fh:
            fh.write(f"hit {self.path}\n")
        objects = _answer(self.path, body)
        self.send_response(200)
        if body.get("stream") is False:
            data = json.dumps(objects[0]).encode()
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_header("content-type", "application/x-ndjson")
        self.send_header("connection", "close")
        self.end_headers()
        for obj in objects:
            self.wfile.write(json.dumps(obj).encode() + b"\n")
            self.wfile.flush()

    def log_message(self, *a):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"
os.environ["OLLAMA_BASE_URL"] = BASE
os.environ["OLLAMA_HOST"] = BASE

import httpx

TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": {
    "type": "object", "properties": {"city": {"type": "string"}}}}}]


def _request(step):
    path = "/api/chat" if step["op"] == "chat" else "/api/generate"
    body = {"model": "fake-model", "options": {"temperature": 0.0, "seed": 1}}
    if step["op"] == "chat":
        body["messages"] = [{"role": "user", "content": "hi"}]
    else:
        body["prompt"] = "hi"
    if step.get("tools"):
        body["tools"] = TOOLS
    if not step.get("stream"):
        body["stream"] = False
    return path, body


def _fold(objects):
    content, calls, final = [], [], {}
    for obj in objects:
        if "error" in obj:
            raise RuntimeError(obj["error"])
        msg = obj.get("message") or {}
        content.append(msg.get("content") or obj.get("response") or "")
        calls.extend(msg.get("tool_calls") or [])
        if obj.get("done"):
            final = obj
    return {"content": "".join(content), "tool_calls": calls,
            "done_reason": final.get("done_reason"), "eval_count": final.get("eval_count"),
            "done": bool(final)}


def raw_sync(step):
    path, body = _request(step)
    if not step.get("stream"):
        return _fold([httpx.post(BASE + path, json=body, timeout=10).json()])
    with httpx.Client(timeout=10) as client:
        with client.stream("POST", BASE + path, json=body) as r:
            objects = []
            for line in r.iter_lines():
                if line:
                    objects.append(json.loads(line))
                    if step.get("abandon"):
                        break
    return _fold(objects)


async def raw_async(step):
    path, body = _request(step)
    async with httpx.AsyncClient(timeout=10) as client:
        if not step.get("stream"):
            return _fold([(await client.post(BASE + path, json=body)).json()])
        objects = []
        async with client.stream("POST", BASE + path, json=body) as r:
            async for line in r.aiter_lines():
                if line:
                    objects.append(json.loads(line))
                    if step.get("abandon"):
                        break
        return _fold(objects)


def _sdk_dump(part):
    return part.model_dump(exclude_none=True)


def sdk(step):
    import ollama
    _, body = _request(step)
    stream = bool(step.get("stream"))
    kwargs = {"model": "fake-model", "stream": stream, "options": body["options"]}
    if step.get("tools"):
        kwargs["tools"] = TOOLS
    if step.get("async"):
        client = ollama.AsyncClient(host=BASE)

        async def go():
            if step["op"] == "chat":
                res = await client.chat(messages=body["messages"], **kwargs)
            else:
                res = await client.generate(prompt="hi", **kwargs)
            if stream:
                return [_sdk_dump(p) async for p in res]
            return [_sdk_dump(res)]
        return _fold(asyncio.run(go()))
    client = ollama.Client(host=BASE)
    if step["op"] == "chat":
        res = client.chat(messages=body["messages"], **kwargs)
    else:
        res = client.generate(prompt="hi", **kwargs)
    return _fold([_sdk_dump(p) for p in res] if stream else [_sdk_dump(res)])


def langchain(step):
    from langchain_ollama import ChatOllama, OllamaLLM
    if step["op"] == "chat":
        model = ChatOllama(model="fake-model", base_url=BASE, temperature=0)
        if step.get("tools"):
            model = model.bind_tools(TOOLS)
        msg = (asyncio.run(model.ainvoke("hi")) if step.get("async") else model.invoke("hi"))
        return {"content": msg.content,
                "tool_calls": [{"name": c["name"], "args": c["args"]} for c in msg.tool_calls]}
    llm = OllamaLLM(model="fake-model", base_url=BASE, temperature=0)
    text = asyncio.run(llm.ainvoke("hi")) if step.get("async") else llm.invoke("hi")
    return {"content": text}


out = []
for step in json.loads(os.environ["AGENT_PLAN"]):
    try:
        client = step.get("client", "raw")
        if client == "ollama":
            out.append(sdk(step))
        elif client == "langchain":
            out.append(langchain(step))
        elif step.get("async"):
            out.append(asyncio.run(raw_async(step)))
        else:
            out.append(raw_sync(step))
    except Exception as exc:
        if not step.get("catch"):
            raise
        out.append({"error": type(exc).__name__, "message": str(exc)[:300]})
with open(os.environ["AGENT_OUT"], "w") as fh:
    json.dump(out, fh)
server.shutdown()
'''


def write_agent(directory: Path) -> Path:
    path = directory / "ollama_agent.py"
    path.write_text(AGENT_SOURCE, encoding="utf-8")
    return path
