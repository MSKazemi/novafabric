"""Configuration review demo using a strictly local synthetic OpenAI-compatible model.

Use with mock_llm_server.py; it never requires a real API key. The SDK
invocation is genuine and can be captured by NovaFabric's OpenAI hook.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from urllib.parse import urlsplit

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
OUTPUTS = pathlib.Path("outputs")
MOCK_MODEL = "novafabric-mock-v1"


def _require_local_model_endpoint() -> None:
    """Fail before constructing a client that might otherwise use real OpenAI."""
    endpoint = os.environ.get("OPENAI_BASE_URL", "")
    url = urlsplit(endpoint)
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("Demo requires OPENAI_BASE_URL pointing to a local HTTP mock server")
    if url.username or url.password or url.fragment or url.query:
        raise RuntimeError("Mock endpoint cannot contain credentials, fragments or queries")
    if url.path not in ("", "/", "/v1", "/v1/"):
        raise RuntimeError("Mock endpoint must use the fixture API path")
    try:
        port = url.port
    except ValueError as exc:
        raise RuntimeError("Invalid local mock server port") from exc
    if port is None:
        raise RuntimeError("Specify the local mock server port in OPENAI_BASE_URL")


def run(mode: str) -> None:
    _require_local_model_endpoint()
    import httpx
    import openai

    config = (FIXTURES / "service.yaml").read_text(encoding="utf-8")
    prompt = (FIXTURES / "prompt.txt").read_text(encoding="utf-8")
    # Avoid ambient HTTP(S)_PROXY variables redirecting the local fixture call.
    with openai.OpenAI(
        api_key="sk-demo-local-only",
        default_headers={"X-Demo-Mode": mode},
        http_client=httpx.Client(trust_env=False, timeout=5.0),
        max_retries=0,
        timeout=5.0,
    ) as client:
        response = client.chat.completions.create(
            model=MOCK_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Config:\n{config}"},
            ],
        )
    raw = response.choices[0].message.content or ""
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        print("agent: synthetic model returned invalid JSON", file=sys.stderr)
        sys.exit(1)

    decision = {"mode": mode, **rec}
    payload = json.dumps(decision, indent=2)
    OUTPUTS.mkdir(exist_ok=True)
    (OUTPUTS / "decision.json").write_text(payload, encoding="utf-8")

    # Preserve the existing BlackBox artifact diff path under nova capture.
    capsule_dir = os.environ.get("NOVAFABRIC_CAPSULE_DIR")
    if capsule_dir:
        cap_out = pathlib.Path(capsule_dir) / "outputs"
        cap_out.mkdir(exist_ok=True)
        (cap_out / "decision.json").write_text(payload, encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demo configuration-review agent")
    parser.add_argument("--mode", choices=["bad", "fixed"], required=True)
    args = parser.parse_args()
    run(args.mode)
