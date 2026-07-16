"""Deterministic dummy 'agent' for the prompt-and-analytics example.

No LLM, no network, no third-party dependencies — stdlib only. The agent
resolves its prompt from the NovaFabric registry at runtime (`nova label get`
+ `nova prompt get`, both experimental), renders it against a fixed support
ticket, and produces a deterministic "answer". When running under
``nova capture`` it self-reports one synthetic model call (tokens, latency,
cost) into ``model-calls.jsonl`` and one tool call into ``tool-calls.jsonl``
via the ``NOVAFABRIC_CAPSULE_DIR`` env var — the same cooperation contract
``examples/eval-and-intervention`` uses.

In a real deployment the wire-level hooks, MCP hook, or `nova mcp-proxy`
record model/tool calls for you; the self-report only keeps this example
runnable offline with zero third-party dependencies.

Prompt selection (env vars):

  PROMPT_ID     prompt identity in the registry   (default: support-triage)
  PROMPT_REF    pin an exact version, e.g. support-triage@2
  PROMPT_LABEL  deployment label to resolve when PROMPT_REF is unset
                (default: production)

The synthetic metrics are a deterministic function of the resolved prompt
version, so two captures of different versions differ in a known way that
`nova query`, `nova trend`, and `nova diff` can surface.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TICKET = "My invoice for June is wrong and I was charged twice."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nova(*args: str) -> dict:
    """Run a `nova` CLI command with --json and parse its output.

    Prefers the `nova` entry point on PATH; falls back to invoking the CLI
    app inside the current interpreter so the example also works when only
    the `novafabric` package is importable.
    """
    nova = shutil.which("nova")
    if nova:
        cmd = [nova, *args, "--json"]
    else:
        cmd = [
            sys.executable,
            "-c",
            "from novafabric.cli.main import app; app()",
            *args,
            "--json",
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"nova {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def resolve_prompt() -> dict:
    """Resolve the prompt version this agent should run with."""
    prompt_id = os.environ.get("PROMPT_ID", "support-triage")
    ref = os.environ.get("PROMPT_REF")
    if not ref:
        label = os.environ.get("PROMPT_LABEL", "production")
        pointer = _nova("label", "get", f"prompt:{prompt_id}", label)
        ref = f"{prompt_id}@{pointer['target_version']}"
    return _nova("prompt", "get", ref)


def triage(rendered_prompt: str) -> dict:
    """Deterministic stand-in for a model call."""
    lowered = rendered_prompt.lower()
    if "invoice" in lowered or "charged" in lowered:
        category = "billing"
    elif "crash" in lowered or "error" in lowered:
        category = "bug"
    else:
        category = "other"
    reply = f"Thanks for reaching out — routing this {category} ticket to the right team."
    if "sla" in lowered:
        reply += " You will hear back within our 24h SLA."
    return {"category": category, "reply": reply}


def main() -> int:
    started = _now_iso()
    t0 = time.monotonic()

    prompt = resolve_prompt()
    version = int(prompt["version"])
    template = prompt["template"]
    rendered = template.replace("{ticket}", TICKET)
    answer = triage(rendered)

    # Deterministic synthetic metrics, a pure function of the prompt version:
    # v2 is a longer template -> more input tokens, higher cost, higher latency.
    input_tokens = len(rendered.split())
    output_tokens = len(answer["reply"].split())
    duration_ms = 90 + 40 * version
    cost = round((input_tokens * 3 + output_tokens * 15) / 1_000_000, 8)

    model_call = {
        "gen_ai.request.model": "example/deterministic-triage",
        "gen_ai.response.model": "example/deterministic-triage",
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "nova.cost": {"currency": "USD", "amount": cost},
    }
    tool_call = {
        "tool_call_id": "tool-0000",
        "tool_name": "prompt_registry_get",
        "input": f"prompt:{prompt['prompt_id']}@{version}",
        "output": {"content_hash": prompt["content_hash"]},
        "status": "success",
        "started": started,
        "finished": _now_iso(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }

    # Self-report into the live capsule (no-op outside `nova capture`).
    capsule_dir = os.environ.get("NOVAFABRIC_CAPSULE_DIR")
    if capsule_dir:
        with (Path(capsule_dir) / "model-calls.jsonl").open("a") as f:
            f.write(json.dumps(model_call) + "\n")
        with (Path(capsule_dir) / "tool-calls.jsonl").open("a") as f:
            f.write(json.dumps(tool_call) + "\n")

    print(
        json.dumps(
            {
                "prompt_ref": f"prompt:{prompt['prompt_id']}@{version}"
                f"+{prompt['content_hash']}",
                "answer": answer,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "duration_ms": duration_ms,
                "cost_usd": cost,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
