"""A workload that talks to an in-process fake AWS Bedrock server (Bedrock replay tests).

The agent starts a stdlib ``http.server`` thread that answers the Bedrock runtime
``Converse`` (JSON) and ``ConverseStream`` (``application/vnd.amazon.eventstream``)
APIs, ``InvokeModel`` and the control-plane ``ListFoundationModels``, points boto3 at
it with ``AWS_ENDPOINT_URL_BEDROCK_RUNTIME`` / ``AWS_ENDPOINT_URL_BEDROCK`` (dummy
credentials, region ``eu-central-1``; never real AWS) and runs the steps in
``AGENT_PLAN``. The server's answers carry ``BEDROCK_CANNED``, so a capture and a
replay run with different values show who answered; every request the server receives
is appended to ``AGENT_SIDE`` (with whether it carried a SigV4 ``Authorization``
header). What the agent observed goes to ``AGENT_OUT`` (a JSON list).

The event-stream encoder here is deliberately independent of NovaFabric's: botocore's
own decoder is the oracle at capture (it parses this server's bytes) and at replay (it
parses the bytes NovaFabric serves).

Plan steps: ``{"op": "converse" | "stream" | "invoke" | "list_models", "tools": bool,
"tool_result": str (send a toolResult turn after a toolUse answer), "model": str,
"abandon": bool, "client": "boto3" | "langchain", "catch": bool}``.
"""

from __future__ import annotations

from pathlib import Path

AGENT_SOURCE = r'''
import binascii, json, os, struct, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

CANNED = os.environ.get("BEDROCK_CANNED", "capture-answer")
SIDE = os.environ["AGENT_SIDE"]
COUNT = {"n": 0, "throttled": set()}
LOCK = threading.Lock()


def _header(name, value):
    name_b, value_b = name.encode(), value.encode()
    return bytes([len(name_b)]) + name_b + b"\x07" + struct.pack(">H", len(value_b)) + value_b


def _message(headers, payload):
    head = b"".join(_header(k, v) for k, v in headers)
    total = 12 + len(head) + len(payload) + 4
    prelude = struct.pack(">II", total, len(head))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    body = prelude + head + payload
    return body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)


def _event(kind, payload):
    data = json.dumps({**payload, "p": "abcdefghij"}).encode()
    return _message([(":event-type", kind), (":content-type", "application/json"),
                     (":message-type", "event")], data)


def _turn(body, n):
    """(text, tool_use) the fake model answers with."""
    messages = body.get("messages") or []
    last = messages[-1]["content"] if messages else []
    if any("toolResult" in block for block in last):
        result = [b["toolResult"] for b in last if "toolResult" in b][0]
        echoed = result["content"][0].get("text", "")
        return f"{CANNED} saw {echoed} #{n}", None
    if body.get("toolConfig"):
        return "", {"toolUseId": f"tooluse_{CANNED}_{n}", "name": "get_weather",
                    "input": {"city": CANNED}}
    return f"{CANNED} #{n}", None


def _usage(n):
    return {"inputTokens": 7, "outputTokens": 3 + n, "totalTokens": 10 + n}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _side(self, what):
        signed = self.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256")
        with open(SIDE, "a") as fh:
            fh.write(f"hit {self.command} {unquote(self.path)} signed={signed} {what}\n")

    def _send(self, status, data, ctype, extra=()):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.send_header("x-amzn-RequestId", f"req-{CANNED}-{COUNT['n']}")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._side("")
        data = json.dumps({"modelSummaries": [{"modelId": "qwen.qwen3-32b-v1:0",
                                               "providerName": CANNED}]}).encode()
        self._send(200, data, "application/json")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        path = unquote(self.path)
        with LOCK:
            COUNT["n"] += 1
            n = COUNT["n"]
        self._side(path)
        parts = path.split("/")
        model, action = parts[2], parts[3]
        if model == "bad-model":
            err = json.dumps({"message": f"{CANNED}: the model is not supported"}).encode()
            return self._send(400, err, "application/json",
                              [("x-amzn-ErrorType", "ValidationException:http://internal/")])
        if model == "throttle-once" and model not in COUNT["throttled"]:
            COUNT["throttled"].add(model)
            err = json.dumps({"message": "Too many requests, please wait"}).encode()
            return self._send(429, err, "application/json",
                              [("x-amzn-ErrorType", "ThrottlingException:http://internal/")])
        body = json.loads(raw or b"{}")
        if action == "invoke":
            return self._send(200, json.dumps({"generation": f"{CANNED} #{n}"}).encode(),
                              "application/json")
        text, tool = _turn(body, n)
        stop = "tool_use" if tool else "end_turn"
        if action == "converse":
            content = [{"toolUse": tool}] if tool else [{"text": text}]
            out = {"output": {"message": {"role": "assistant", "content": content}},
                   "stopReason": stop, "usage": _usage(n), "metrics": {"latencyMs": 100 + n}}
            return self._send(200, json.dumps(out).encode(), "application/json")
        events = [_event("messageStart", {"role": "assistant"})]
        if tool:
            events.append(_event("contentBlockStart", {"contentBlockIndex": 0, "start": {
                "toolUse": {"toolUseId": tool["toolUseId"], "name": tool["name"]}}}))
            raw_input = json.dumps(tool["input"])
            for piece in (raw_input[:5], raw_input[5:]):
                events.append(_event("contentBlockDelta", {
                    "contentBlockIndex": 0, "delta": {"toolUse": {"input": piece}}}))
        else:
            for piece in (text[: len(text) // 2], text[len(text) // 2:]):
                events.append(_event("contentBlockDelta", {
                    "contentBlockIndex": 0, "delta": {"text": piece}}))
        events.append(_event("contentBlockStop", {"contentBlockIndex": 0}))
        events.append(_event("messageStop", {"stopReason": stop}))
        events.append(_event("metadata", {"usage": _usage(n),
                                          "metrics": {"latencyMs": 100 + n}}))
        self._send(200, b"".join(events), "application/vnd.amazon.eventstream")

    def log_message(self, *a):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"
os.environ["AWS_ENDPOINT_URL_BEDROCK_RUNTIME"] = BASE
os.environ["AWS_ENDPOINT_URL_BEDROCK"] = BASE
os.environ.setdefault("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "fake-secret")
os.environ["AWS_DEFAULT_REGION"] = "eu-central-1"
os.environ.pop("AWS_PROFILE", None)

import boto3

MODEL = "qwen.qwen3-32b-v1:0"
TOOL_CONFIG = {"tools": [{"toolSpec": {"name": "get_weather", "description": "weather",
               "inputSchema": {"json": {"type": "object",
                                        "properties": {"city": {"type": "string"}}}}}}]}
TOOL_RESULT = os.environ.get("BEDROCK_TOOL_RESULT", "sunny")


def _client(service="bedrock-runtime"):
    return boto3.client(service, region_name="eu-central-1")


def _kwargs(step, messages):
    kwargs = {"modelId": step.get("model", MODEL), "messages": messages,
              "inferenceConfig": {"maxTokens": 64, "temperature": 0.0, "topP": 0.9},
              "system": [{"text": "be brief"}]}
    if step.get("tools"):
        kwargs["toolConfig"] = TOOL_CONFIG
    return kwargs


def _fold_stream(response, abandon):
    events = []
    stream = response["stream"]
    for event in stream:
        events.append(event)
        if abandon:
            stream.close()
            break
    return events


def _call(client, step, messages):
    if step["op"] == "converse":
        res = client.converse(**_kwargs(step, messages))
        return {"output": res["output"], "stopReason": res["stopReason"], "usage": res["usage"],
                "metrics": res["metrics"],
                "request_id": res["ResponseMetadata"]["RequestId"]}
    res = client.converse_stream(**_kwargs(step, messages))
    return {"events": _fold_stream(res, step.get("abandon")),
            "request_id": res["ResponseMetadata"]["RequestId"]}


def _assistant_from(result):
    if "output" in result:
        return result["output"]["message"]
    content, tool, raw = [], None, ""
    for event in result["events"]:
        if "contentBlockStart" in event:
            tool = dict(event["contentBlockStart"]["start"]["toolUse"])
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        raw += delta.get("toolUse", {}).get("input", "")
    tool["input"] = json.loads(raw)
    return {"role": "assistant", "content": [{"toolUse": tool}]}


def boto(step):
    if step["op"] == "list_models":
        res = _client("bedrock").list_foundation_models()
        return {"models": res["modelSummaries"]}
    client = _client()
    if step["op"] == "invoke":
        res = client.invoke_model(modelId=step.get("model", MODEL),
                                  body=json.dumps({"prompt": "hi"}))
        return json.loads(res["body"].read())
    messages = [{"role": "user", "content": [{"text": "hi"}]}]
    first = _call(client, step, messages)
    if "tool_result" not in step:
        return first
    tool = [b["toolUse"] for b in _assistant_from(first)["content"] if "toolUse" in b][0]
    messages += [_assistant_from(first), {"role": "user", "content": [{"toolResult": {
        "toolUseId": tool["toolUseId"], "content": [{"text": TOOL_RESULT}]}}]}]
    return {"first": first, "second": _call(client, step, messages)}


def langchain(step):
    from langchain_aws import ChatBedrockConverse
    from langchain_core.messages import HumanMessage, ToolMessage
    model = ChatBedrockConverse(model=step.get("model", MODEL), region_name="eu-central-1",
                                temperature=0, max_tokens=64,
                                disable_streaming=step["op"] != "stream")
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "weather",
              "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    bound = model.bind_tools(tools) if step.get("tools") else model
    history = [HumanMessage("hi")]
    msg = bound.invoke(history)
    out = {"content": msg.content, "tool_calls": [
        {"name": c["name"], "args": c["args"], "id": c["id"]} for c in msg.tool_calls]}
    if "tool_result" in step and msg.tool_calls:
        history += [msg, ToolMessage(TOOL_RESULT, tool_call_id=msg.tool_calls[0]["id"])]
        out["second"] = bound.invoke(history).content
    return out


out = []
for step in json.loads(os.environ["AGENT_PLAN"]):
    try:
        out.append(langchain(step) if step.get("client") == "langchain" else boto(step))
    except Exception as exc:
        if not step.get("catch"):
            raise
        out.append({"error": type(exc).__name__, "message": str(exc)[:300]})
with open(os.environ["AGENT_OUT"], "w") as fh:
    json.dump(out, fh, default=str)
server.shutdown()
'''


def write_agent(directory: Path) -> Path:
    path = directory / "bedrock_agent.py"
    path.write_text(AGENT_SOURCE, encoding="utf-8")
    return path
