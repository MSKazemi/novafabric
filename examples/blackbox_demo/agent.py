"""Demo agent: reads config fixture, calls mocked model, writes decision.json.

Usage:
    python agent.py --mode bad     # risky recommendation (disable rate-limiting)
    python agent.py --mode fixed   # safe recommendation (reduce max_connections)

The openai SDK call is automatically captured by `nova capture` via the
OpenAI hook installed in src/novafabric/capture/hooks/_openai.py.
Set OPENAI_BASE_URL=http://127.0.0.1:9099 to point at mock_llm_server.py.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

import openai

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
OUTPUTS = pathlib.Path("outputs")


def run(mode: str) -> None:
    client = openai.OpenAI(
        default_headers={"X-Demo-Mode": mode},
    )
    config = (FIXTURES / "service.yaml").read_text()
    prompt = (FIXTURES / "prompt.txt").read_text()

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Config:\n{config}"},
        ],
    )
    raw = response.choices[0].message.content or ""

    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        print(f"agent: model returned non-JSON: {raw!r}", file=sys.stderr)
        sys.exit(1)

    decision = {"mode": mode, **rec}
    payload = json.dumps(decision, indent=2)

    OUTPUTS.mkdir(exist_ok=True)
    (OUTPUTS / "decision.json").write_text(payload)

    # When running under `nova capture`, also copy decision.json into the
    # capsule outputs directory so it appears in `nova diff` artifact diffs.
    capsule_dir = os.environ.get("NOVAFABRIC_CAPSULE_DIR")
    if capsule_dir:
        cap_out = pathlib.Path(capsule_dir) / "outputs"
        cap_out.mkdir(exist_ok=True)
        (cap_out / "decision.json").write_text(payload)

    print(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demo config-review agent")
    parser.add_argument("--mode", choices=["bad", "fixed"], required=True)
    args = parser.parse_args()
    run(args.mode)
