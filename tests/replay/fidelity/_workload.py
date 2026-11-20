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


def _handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    prompt = body.get("messages", [{}])[-1].get("content", "")
    _append("FIDELITY_MODEL_HITS", f"{PHASE} openai {request.url.path} {prompt}")
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-fidelity",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-fidelity",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": _answer(prompt)},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


@record.tool(mutation_class="read-only")
def lookup(x: int) -> dict:
    _append("FIDELITY_SENTINEL", f"{PHASE} lookup {x}")
    return {"v": x * 2}


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
        elif step == "tool":
            out["tool"] = lookup(21)
        elif step == "net":
            _net(out)
        elif step == "child_net":
            _child_net(out)
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
