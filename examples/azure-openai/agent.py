"""Azure OpenAI example: capture a chat completion against a Microsoft
Azure deployment of the OpenAI API.

The Azure path uses `openai.AzureOpenAI`, which is the same client
class as the public OpenAI SDK with different config — so NovaFabric's
existing openai-SDK capture hook fires identically. The proof point is:
NovaFabric is provider-endpoint-agnostic; it captures whatever the
openai SDK emits, regardless of which back-end you're hitting.

Run:
    export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
    export AZURE_OPENAI_API_KEY=<your-azure-key>
    export AZURE_OPENAI_DEPLOYMENT=<your-deployment-name>   # e.g. gpt-4o-mini
    uv pip install openai
    uv run nova capture --output-dir examples/azure-openai/runs python examples/azure-openai/agent.py

Skips cleanly if any of the three env vars or the openai SDK is missing.
"""
from __future__ import annotations

import os
import sys


REQUIRED_ENV = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT")


def _have_prereqs() -> tuple[bool, str]:
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        return False, f"env vars not set: {', '.join(missing)}"
    try:
        import openai  # noqa: F401
    except ImportError:
        return False, "`openai` SDK not installed — run `uv pip install openai`."
    return True, ""


def _call_azure() -> str:
    from openai import AzureOpenAI

    # AzureOpenAI auto-reads AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY.
    client = AzureOpenAI(api_version=os.environ.get("OPENAI_API_VERSION", "2024-10-21"))
    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        max_tokens=64,
        messages=[
            {"role": "user", "content": "In one sentence: what is replayable AI infrastructure?"},
        ],
    )
    text = response.choices[0].message.content or ""
    print(f"Model said: {text}")
    return text


def main() -> int:
    ok, why = _have_prereqs()
    if not ok:
        print(f"[azure-openai] {why}")
        print("[azure-openai] Skipping cleanly — exit 0.")
        return 0

    _call_azure()
    print("\nInspect the capsule:")
    print("  ls .novafabric/runs/  # or your --output-dir")
    print("  cat <capsule>/model-calls.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
