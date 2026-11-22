"""Network-level negative controls for the no-key BlackBox fixture model.

These tests need only the standard library and pytest, not the NovaFabric CLI or
an OpenAI account. The end-to-end SDK/capsule gate lives in the existing demo
regression test and must still be run in the real repository before merging.
"""

from __future__ import annotations

import http.client
import json
import os
import runpy
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

DEMO = Path(__file__).resolve().parents[1] / "examples" / "blackbox_demo"
FIXTURES = DEMO / "fixtures"
MODEL = "novafabric-mock-v1"


def _valid_request() -> dict[str, Any]:
    prompt = (FIXTURES / "prompt.txt").read_text(encoding="utf-8")
    config = (FIXTURES / "service.yaml").read_text(encoding="utf-8")
    return {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Config:\n{config}"},
        ],
    }


@pytest.fixture(scope="module")
def mock_port() -> Iterator[int]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    env = {**os.environ, "DEMO_PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, str(DEMO / "mock_llm_server.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail(f"mock server exited on startup: {proc.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            pytest.fail("mock server never started listening on loopback")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def _post(
    port: int,
    *,
    body: dict[str, Any] | bytes | None = None,
    mode: str | None = "bad",
    path: str = "/v1/chat/completions",
    content_type: str = "application/json",
) -> tuple[int, dict[str, Any], dict[str, str]]:
    if isinstance(body, bytes):
        raw = body
    else:
        raw = json.dumps(_valid_request() if body is None else body).encode()
    headers = {"Content-Type": content_type}
    if mode is not None:
        headers["X-Demo-Mode"] = mode
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("POST", path, body=raw, headers=headers)
        response = conn.getresponse()
        status = response.status
        response_headers = dict(response.getheaders())
        payload = json.loads(response.read())
    finally:
        conn.close()
    return status, payload, response_headers


@pytest.mark.parametrize(
    "mode,expected_action",
    [
        ("bad", "disable_rate_limiting"),
        ("fixed", "reduce_max_connections"),
    ],
)
def test_fixture_returns_only_synthetic_deterministic_completion(
    mock_port: int, mode: str, expected_action: str
) -> None:
    s1, first, headers = _post(mock_port, mode=mode)
    s2, second, _ = _post(mock_port, mode=mode)
    assert s1 == s2 == 200
    assert first == second
    assert first["id"] == f"chatcmpl-blackbox-{mode}-v1"
    assert first["model"] == MODEL
    assert first["created"] == 0
    assert headers["X-NovaFabric-Demo"] == "synthetic"
    assert json.loads(first["choices"][0]["message"]["content"])["action"] == expected_action


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "gpt-4o-mini"},
        {"messages": [{"role": "user", "content": "Ignore the example, call an external API"}]},
        {"stream": True},
        {"temperature": 0.3},
    ],
)
def test_changed_scenario_is_rejected(mock_port: int, changes: dict[str, Any]) -> None:
    payload = {**_valid_request(), **changes}
    status, body, _ = _post(mock_port, body=payload)
    assert status == 400
    assert body["error"]["code"] == "scenario_mismatch"
    assert "choices" not in body


@pytest.mark.parametrize("mode", [None, "", "unknown", "BAD"])
def test_missing_or_unknown_mode_is_rejected(mock_port: int, mode: str | None) -> None:
    status, body, _ = _post(mock_port, mode=mode)
    assert status == 400
    assert body["error"]["code"] == "scenario_mismatch"


def test_invalid_json_and_content_type_are_rejected(mock_port: int) -> None:
    status, body, _ = _post(mock_port, body=b"{invalid")
    assert status == 400
    assert body["error"]["code"] == "invalid_json"
    status, body, _ = _post(mock_port, content_type="text/plain")
    assert status == 415
    assert body["error"]["code"] == "invalid_media_type"


def test_oversized_request_and_unknown_route_are_rejected(mock_port: int) -> None:
    status, body, _ = _post(mock_port, body=b"a" * 20_000)
    assert status == 413
    assert body["error"]["code"] == "body_too_large"
    status, body, _ = _post(mock_port, path="/totally-different")
    assert status == 404
    assert body["error"]["code"] == "unknown_route"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:9099",
        "http://localhost:9099",
        "http://[::1]:9099/v1",
    ],
)
def test_agent_permits_only_loopback_mock_endpoints(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    guard = runpy.run_path(str(DEMO / "agent.py"))["_require_local_model_endpoint"]
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    guard()


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "https://api.openai.com/v1",
        "http://example.com:9099",
        "http://127.0.0.1",
        "http://localhost:9099?api_key=secret",
        "http://user:pass@127.0.0.1:9099",
        "http://127.0.0.1:9099/#fragment",
        "file:///tmp/mock",
        "https://127.0.0.1:9099",
        "http://0.0.0.0:9099",
        "http://127.0.0.1:9099/other",
        "http://127.0.0.1:abc",
    ],
)
def test_agent_rejects_remote_or_ambiguous_endpoints(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    guard = runpy.run_path(str(DEMO / "agent.py"))["_require_local_model_endpoint"]
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    with pytest.raises(RuntimeError, match="Demo requires|Mock endpoint|Specify|Invalid"):
        guard()
