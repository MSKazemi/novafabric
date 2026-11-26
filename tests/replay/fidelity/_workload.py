"""Fidelity-corpus workload: run as a script under ``nova capture`` / ``nova replay``.

Every observable effect is written to a file or a socket the *harness* owns, never
read back from NovaFabric:

- ``FIDELITY_MARKERS``: ``start <phase>`` on entry, ``end <phase> rc=<n>`` just before exit;
- ``FIDELITY_MODEL_HITS``: one line per request that reached the in-process fake
  OpenAI transport (a served replay never reaches it);
- ``FIDELITY_SENTINEL``: one line per run of a ``record.tool`` body;
- ``FIDELITY_SINK_PORT``: a loopback TCP sink that counts accepted connections;
- ``FIDELITY_OUTSIDE``: a directory outside the work dir (file side effects);
- ``AGENT_OUT``: the workload's final output (digested by the outcome oracle).

The fake transport answers ``capture-answer:<prompt>`` when ``FIDELITY_PHASE`` is
``capture`` and ``FIDELITY_CANARY:<prompt>`` otherwise, so a replay output carrying the
canary was answered live.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

PHASE = os.environ["FIDELITY_PHASE"]
STEPS = [s for s in os.environ.get("FIDELITY_STEPS", "").split(",") if s]


def _append(var: str, line: str) -> None:
    with open(os.environ[var], "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


_append("FIDELITY_MARKERS", f"start {PHASE}")

import httpx  # noqa: E402 -- the start marker must precede every import that can fail

from novafabric.capture import record  # noqa: E402


def _answer(prompt: str) -> str:
    prefix = "capture-answer" if PHASE == "capture" else os.environ["FIDELITY_CANARY"]
    return f"{prefix}:{prompt}"


def _prompt(body: dict) -> str:
    """The prompt as each API shape carries it (chat ``messages`` or Responses ``input``)."""
    if "messages" in body:
        return body["messages"][-1].get("content", "")
    return body.get("input", "")


def _sse(events: list[dict]) -> httpx.Response:
    payload = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(
        200, content=payload.encode(), headers={"content-type": "text/event-stream"}
    )


def _handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    prompt = _prompt(body)
    _append("FIDELITY_MODEL_HITS", f"{PHASE} openai {request.url.path} {prompt}")
    text = _answer(prompt)
    if request.url.path.endswith("/responses"):
        return httpx.Response(
            200,
            json={
                "id": "resp-fidelity",
                "object": "response",
                "created_at": 0,
                "model": "gpt-fidelity",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg-fidelity",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                ],
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
                "error": None,
                "incomplete_details": None,
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )
    base = {
        "id": "chatcmpl-fidelity",
        "created": 0,
        "model": "gpt-fidelity",
    }
    if body.get("stream"):

        def chunk(delta: dict, finish: str | None = None) -> dict:
            choice = {"index": 0, "delta": delta, "finish_reason": finish}
            return {**base, "object": "chat.completion.chunk", "choices": [choice]}

        half = len(text) // 2
        return _sse(
            [
                chunk({"role": "assistant", "content": ""}),
                chunk({"content": text[:half]}),
                chunk({"content": text[half:]}),
                chunk({}, "stop"),
            ]
        )
    return httpx.Response(
        200,
        json={
            **base,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


@record.tool(mutation_class="read-only")
def lookup(x: int) -> dict:
    _append("FIDELITY_SENTINEL", f"{PHASE} lookup {x}")
    return {"v": x * 2}


def _force_live(client_http: httpx.Client, prompt: str) -> str:
    """Control: bypass the SDK so no dispatcher can serve this call (answered by the canary)."""
    resp = client_http.post(
        "http://model.invalid/v1/chat/completions",
        json={"model": "gpt-fidelity", "messages": [{"role": "user", "content": prompt}]},
    )
    return resp.json()["choices"][0]["message"]["content"]


def _live_requested(i: int) -> bool:
    return i == 0 and os.environ.get("FIDELITY_FORCE_LIVE_MODEL") == "1"


def _model(out: dict) -> None:
    import openai

    http_client = httpx.Client(transport=httpx.MockTransport(_handler))
    client = openai.OpenAI(
        api_key="sk-test-not-a-key", base_url="http://model.invalid/v1", http_client=http_client
    )
    answers = []
    for i, prompt in enumerate(("q1", "q2")):
        if i == 0 and os.environ.get("FIDELITY_FORCE_LIVE_MODEL") == "1":
            # Control: bypass the SDK so no dispatcher can serve this call.
            resp = http_client.post(
                "http://model.invalid/v1/chat/completions",
                json={"model": "gpt-fidelity", "messages": [{"role": "user", "content": prompt}]},
            )
            answers.append(resp.json()["choices"][0]["message"]["content"])
            continue
        r = client.chat.completions.create(
            model="gpt-fidelity", messages=[{"role": "user", "content": prompt}]
        )
        answers.append(r.choices[0].message.content)
    out["model"] = answers


def _openai_kwargs() -> dict:
    return {"api_key": "sk-test-not-a-key", "base_url": "http://model.invalid/v1"}


def _model_async(out: dict) -> None:
    import asyncio

    import openai

    async def run() -> list[str]:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        client = openai.AsyncOpenAI(http_client=http_client, **_openai_kwargs())
        answers = []
        for i, prompt in enumerate(("q1", "q2")):
            if _live_requested(i):
                answers.append(
                    _force_live(httpx.Client(transport=httpx.MockTransport(_handler)), prompt)
                )
                continue
            r = await client.chat.completions.create(
                model="gpt-fidelity", messages=[{"role": "user", "content": prompt}]
            )
            answers.append(r.choices[0].message.content)
        return answers

    out["model_async"] = asyncio.run(run())


def _model_stream(out: dict) -> None:
    import openai

    http_client = httpx.Client(transport=httpx.MockTransport(_handler))
    client = openai.OpenAI(http_client=http_client, **_openai_kwargs())
    answers = []
    for i, prompt in enumerate(("q1", "q2")):
        if _live_requested(i):
            answers.append(_force_live(http_client, prompt))
            continue
        stream = client.chat.completions.create(
            model="gpt-fidelity",
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        )
        answers.append("".join(c.choices[0].delta.content or "" for c in stream if c.choices))
    out["model_stream"] = answers


def _model_responses(out: dict) -> None:
    import openai

    http_client = httpx.Client(transport=httpx.MockTransport(_handler))
    client = openai.OpenAI(http_client=http_client, **_openai_kwargs())
    answers = []
    for i, prompt in enumerate(("q1", "q2")):
        if _live_requested(i):
            answers.append(_force_live(http_client, prompt))
            continue
        r = client.responses.create(model="gpt-fidelity", input=prompt)
        answers.append(r.output_text)
    out["model_responses"] = answers


def _net(out: dict) -> None:
    import socket

    port = int(os.environ["FIDELITY_SINK_PORT"])
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        pass
    out["net"] = "connected"


def _child_net(out: dict) -> None:
    # A non-Python child: replay's in-process socket observer cannot see it (#52).
    port = int(os.environ["FIDELITY_SINK_PORT"])
    proc = subprocess.run(
        ["bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{port} && exec 3>&-"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    out["child_net_rc"] = proc.returncode


def _py_child_net(out: dict) -> None:
    # A *Python* child: the support matrix says replay observes its connections (ADR-0304).
    port = int(os.environ["FIDELITY_SINK_PORT"])
    code = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=5).close()"
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=30, check=False
    )
    out["py_child_net_rc"] = proc.returncode


def _file(out: dict, per_phase: bool = False) -> None:
    # per_phase: a new file each run (a timestamped report, say) -- not a repeat of
    # a recorded write, so the replay's file_effects check does not see it.
    name = f"ledger.{PHASE}.txt" if per_phase else "ledger.txt"
    path = os.path.join(os.environ["FIDELITY_OUTSIDE"], name)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("row\n")
    out["file"] = "written"


def _ollama(out: dict, path: str = "/api/chat") -> None:
    base = os.environ["OLLAMA_BASE_URL"]
    resp = httpx.post(
        f"{base}{path}",
        json={
            "model": "fidelity-model",
            "messages": [{"role": "user", "content": "q1"}],
            "stream": False,
        },
        timeout=10,
    )
    out["ollama"] = resp.json()["message"]["content"]


def main() -> int:
    out: dict = {}
    for step in STEPS:
        if step == "model":
            _model(out)
        elif step == "model_async":
            _model_async(out)
        elif step == "model_stream":
            _model_stream(out)
        elif step == "model_responses":
            _model_responses(out)
        elif step == "tool":
            out["tool"] = lookup(21)
        elif step == "net":
            _net(out)
        elif step == "child_net":
            _child_net(out)
        elif step == "py_child_net":
            _py_child_net(out)
        elif step == "file":
            _file(out)
        elif step == "file_per_phase":
            _file(out, per_phase=True)
        elif step == "ollama":
            _ollama(out)
        elif step == "ollama_embed":  # runs a model; not a surface replay serves
            _ollama(out, "/api/embed")
        elif step != "exit1":
            raise SystemExit(f"unknown step {step!r}")
    if os.environ.get("FIDELITY_MUTATE_OUTPUT") == "1":
        out["mutated"] = True  # control: the outcome oracle must notice
    with open(os.environ["AGENT_OUT"], "w", encoding="utf-8") as fh:
        json.dump(out, fh, sort_keys=True)
    rc = 1 if "exit1" in STEPS else 0
    _append("FIDELITY_MARKERS", f"end {PHASE} rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
