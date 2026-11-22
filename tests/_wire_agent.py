"""A workload that talks to in-process fake model providers over raw HTTP (ADR-0324 tests).

The agent starts a stdlib ``http.server`` thread on ``127.0.0.1:0`` that answers three
provider shapes, each under its own path prefix so a test URL registry can classify it
(:func:`write_registry`):

* ``/mistral/v1/chat/completions`` -- Mistral-shaped chat completion (JSON, or SSE when
  the body says ``"stream": true``);
* ``/gemini/v1beta/models/<model>:generateContent`` and ``:streamGenerateContent?alt=sse``
  -- Gemini-REST-shaped;
* ``/local/v1/chat/completions`` -- an OpenAI-compatible local server (vLLM, llama.cpp).

Answers carry ``WIRE_CANNED``, so a capture and a replay run with different values show
who answered; every request the server receives is appended to ``AGENT_SIDE``. What the
agent observed goes to ``AGENT_OUT`` (a JSON list, one entry per plan step).

Plan steps (``AGENT_PLAN``): ``{"api": "mistral" | "gemini" | "local", "client": "httpx" |
"httpx-async" | "requests", "stream": bool, "prompt": str, "fail_first": int,
"catch": bool}``. ``fail_first: n`` makes the server answer the step's first n requests
with HTTP 429 (the agent retries). ``WIRE_TOKEN`` and ``WIRE_REQUEST_ID`` are sent as
``authorization`` / ``x-request-id`` headers and as Gemini's ``key`` query parameter.
"""

from __future__ import annotations

from pathlib import Path

AGENT_SOURCE = r'''
import asyncio, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CANNED = os.environ.get("WIRE_CANNED", "capture-answer")
SIDE = os.environ["AGENT_SIDE"]
COUNT = {"n": 0}
FAILS = {}


def _chat_chunks(text, model):
    words = text.split(" ")
    out = []
    for i, w in enumerate(words):
        piece = w if i == 0 else " " + w
        out.append({"id": "cmpl-1", "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
    out.append({"id": "cmpl-1", "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": len(words)}})
    return out


def _gemini_parts(text):
    return [{"candidates": [{"content": {"role": "model", "parts": [{"text": t}]}}]}
            for t in text.split(" ")]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        body = json.loads(raw or b"{}")
        with open(SIDE, "a") as fh:
            fh.write(f"hit {self.path}\n")
        prompt = json.dumps(body, sort_keys=True)
        fail_key = prompt
        if FAILS.get(fail_key, 0) < int(body.get("fail_first") or 0):
            FAILS[fail_key] = FAILS.get(fail_key, 0) + 1
            data = json.dumps({"message": "rate limited"}).encode()
            self.send_response(429)
            self.send_header("content-type", "application/json")
            self.send_header("retry-after", "0")
            self.send_header("set-cookie", "session=secret-cookie")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        COUNT["n"] += 1
        text = f"{CANNED} number {COUNT['n']}"
        model = body.get("model", "m")
        gemini = self.path.startswith("/gemini/")
        stream = ("streamGenerateContent" in self.path) if gemini else bool(body.get("stream"))
        if not stream:
            if gemini:
                obj = {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                                       "finishReason": "STOP"}],
                       "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 3}}
            else:
                obj = {"id": "cmpl-1", "object": "chat.completion", "model": model,
                       "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                    "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 5, "completion_tokens": 4}}
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("x-request-id", f"req-{COUNT['n']}")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        events = _gemini_parts(text) if gemini else _chat_chunks(text, model)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()
        for ev in events:
            self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
            self.wfile.flush()
            time.sleep(0.01)
        if not gemini:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        self.close_connection = True

    def log_message(self, *a):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"

import httpx
import requests

TOKEN = os.environ.get("WIRE_TOKEN", "tok-1")
REQ_ID = os.environ.get("WIRE_REQUEST_ID", "rid-1")


def _request(step):
    api = step["api"]
    prompt = step.get("prompt", "hi")
    stream = bool(step.get("stream"))
    headers = {"authorization": f"Bearer {TOKEN}", "x-request-id": REQ_ID}
    if api == "gemini":
        verb = "streamGenerateContent" if stream else "generateContent"
        url = f"{BASE}/gemini/v1beta/models/gemini-x:{verb}"
        params = {"key": TOKEN}
        if stream:
            params["alt"] = "sse"
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    else:
        url = f"{BASE}/{api}/v1/chat/completions"
        params = {}
        body = {"model": "mistral-small" if api == "mistral" else "local-llama",
                "messages": [{"role": "user", "content": prompt}], "temperature": 0}
        if stream:
            body["stream"] = True
    if step.get("fail_first"):
        body["fail_first"] = step["fail_first"]
    return url, params, headers, body


def _text_of(api, obj):
    if api == "gemini":
        return "".join(p.get("text", "") for c in obj.get("candidates", [])
                       for p in c.get("content", {}).get("parts", []))
    choice = obj["choices"][0]
    return (choice.get("message") or choice.get("delta") or {}).get("content") or ""


def _fold_sse(api, lines):
    events, text = [], []
    for line in lines:
        if not line.startswith("data: "):
            continue
        data = line[len("data: "):]
        events.append(data)
        if data != "[DONE]":
            text.append(_text_of(api, json.loads(data)))
    return {"text": "".join(text), "events": len(events)}


def _with_retry(send):
    for _ in range(5):
        status, result = send()
        if status != 429:
            return {"status": status, **result}
    return {"status": status, **result}


def via_httpx(step):
    url, params, headers, body = _request(step)
    api = step["api"]

    def send():
        with httpx.Client(timeout=10) as client:
            if not step.get("stream"):
                r = client.post(url, params=params, headers=headers, json=body)
                result = {"text": _text_of(api, r.json())} if r.status_code == 200 else {}
                return r.status_code, result
            with client.stream("POST", url, params=params, headers=headers, json=body) as r:
                chunks = [c.decode() for c in r.iter_raw()]
            lines = "".join(chunks).splitlines()
            return r.status_code, {**_fold_sse(api, lines), "chunks": chunks}
    return _with_retry(send)


async def via_httpx_async(step):
    url, params, headers, body = _request(step)
    api = step["api"]
    async with httpx.AsyncClient(timeout=10) as client:
        for _ in range(5):
            if not step.get("stream"):
                r = await client.post(url, params=params, headers=headers, json=body)
                if r.status_code == 429:
                    continue
                return {"status": r.status_code, "text": _text_of(api, r.json())}
            async with client.stream("POST", url, params=params, headers=headers, json=body) as r:
                chunks = [c.decode() async for c in r.aiter_raw()]
            if r.status_code == 429:
                continue
            lines = "".join(chunks).splitlines()
            return {"status": r.status_code, **_fold_sse(api, lines), "chunks": chunks}


def via_requests(step):
    url, params, headers, body = _request(step)
    api = step["api"]

    def send():
        if not step.get("stream"):
            r = requests.post(url, params=params, headers=headers, json=body, timeout=10)
            result = {"text": _text_of(api, r.json())} if r.status_code == 200 else {}
            return r.status_code, result
        with requests.post(url, params=params, headers=headers, json=body, timeout=10,
                           stream=True) as r:
            lines = [ln for ln in r.iter_lines(decode_unicode=True)]
        return r.status_code, _fold_sse(api, lines)
    return _with_retry(send)


out = []
for step in json.loads(os.environ["AGENT_PLAN"]):
    try:
        client = step.get("client", "httpx")
        if client == "requests":
            out.append(via_requests(step))
        elif client == "httpx-async":
            out.append(asyncio.run(via_httpx_async(step)))
        else:
            out.append(via_httpx(step))
    except Exception as exc:
        if not step.get("catch"):
            raise
        out.append({"error": type(exc).__name__, "message": str(exc)[:300]})
with open(os.environ["AGENT_OUT"], "w") as fh:
    json.dump(out, fh)
server.shutdown()
'''

#: A URL registry for these tests: each fake provider under its own path prefix.
REGISTRY = """\
schema_version: "0.1.0"
patterns:
  - match: '^http://127\\.0\\.0\\.1:[0-9]+/mistral/'
    match_kind: regex
    gen_ai_system: "mistral"
  - match: '^http://127\\.0\\.0\\.1:[0-9]+/gemini/'
    match_kind: regex
    gen_ai_system: "gemini"
  - match: '^http://127\\.0\\.0\\.1:[0-9]+/local/'
    match_kind: regex
    gen_ai_system: "local.openai-compatible"
"""


def write_agent(directory: Path) -> Path:
    path = directory / "wire_agent.py"
    path.write_text(AGENT_SOURCE, encoding="utf-8")
    return path


def write_registry(directory: Path) -> Path:
    path = directory / "url_registry.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    return path
