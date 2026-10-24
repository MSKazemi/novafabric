"""A wire call that never got a response is never recorded as a success.

Review capture-runners-05: the httpx, aiohttp, urllib3 and requests hooks caught only
``Exception``, so ``asyncio.CancelledError`` (``wait_for`` timeouts, task
cancellation) and ``KeyboardInterrupt`` — ``BaseException`` — left ``error_status``
unset and ``response`` ``None``. The record said ``status: success``, and aiohttp and
urllib3 defaulted the missing response to HTTP 200, so an abandoned call counted as a
successful one in cost and latency reports. The interruption itself must still
propagate unchanged.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from novafabric.capture.capsule import CapsuleWriter

RUN_ID = "01HXTEST000000000000000000"
URL = "https://api.openai.com/v1/chat/completions"
BODY = b'{"model":"gpt-4o","messages":[]}'


@pytest.fixture(autouse=True)
def _clean_global_hook_state() -> None:
    from novafabric.capture.event_recorder import set_current_recorder
    from novafabric.capture.hooks import uninstall_all

    uninstall_all()
    set_current_recorder(None)


@pytest.fixture
def writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def _records(writer: CapsuleWriter) -> list[dict[str, Any]]:
    text = (writer.capsule_dir / "model-calls.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


INTERRUPTS = [asyncio.CancelledError, KeyboardInterrupt]


def _raise(exc: type[BaseException]) -> Any:
    def original(*_a: Any, **_k: Any) -> Any:
        raise exc()

    return original


def _araise(exc: type[BaseException]) -> Any:
    async def original(*_a: Any, **_k: Any) -> Any:
        raise exc()

    return original


@pytest.mark.parametrize("exc", INTERRUPTS)
def test_httpx_sync(writer: CapsuleWriter, exc: type[BaseException]) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    req = MagicMock(content=BODY)
    req.url.__str__ = MagicMock(return_value=URL)
    with pytest.raises(exc):
        HttpxHook(writer=writer, parent_span_id="0" * 16)._wrapped_send(req, original=_raise(exc))
    assert [r["status"] for r in _records(writer)] == ["error"]


@pytest.mark.parametrize("exc", INTERRUPTS)
def test_httpx_async(writer: CapsuleWriter, exc: type[BaseException]) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    req = MagicMock(content=BODY)
    req.url.__str__ = MagicMock(return_value=URL)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)
    with pytest.raises(exc):
        asyncio.run(hook._wrapped_send_async(req, original=_araise(exc)))
    assert [r["status"] for r in _records(writer)] == ["error"]


@pytest.mark.parametrize("exc", INTERRUPTS)
def test_aiohttp(writer: CapsuleWriter, exc: type[BaseException]) -> None:
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)
    with pytest.raises(exc):
        asyncio.run(hook._wrapped_request(
            "POST", URL, original=_araise(exc), json=json.loads(BODY)
        ))
    assert [r["status"] for r in _records(writer)] == ["error"]


@pytest.mark.parametrize("exc", INTERRUPTS)
def test_urllib3(writer: CapsuleWriter, exc: type[BaseException]) -> None:
    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    pool = MagicMock(scheme="https", host="api.openai.com", port=443)
    hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)
    with pytest.raises(exc):
        hook._wrapped_urlopen(pool, "POST", "/v1/chat/completions", original=_raise(exc), body=BODY)
    assert [r["status"] for r in _records(writer)] == ["error"]


@pytest.mark.parametrize("exc", INTERRUPTS)
def test_requests(writer: CapsuleWriter, exc: type[BaseException]) -> None:
    from novafabric.capture.hooks._requests import RequestsHook

    prepared = MagicMock(url=URL, body=BODY)
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16)
    with pytest.raises(exc):
        hook._wrapped_send(prepared, original=_raise(exc))
    assert [r["status"] for r in _records(writer)] == ["error"]


def test_a_missing_response_is_not_http_200(writer: CapsuleWriter) -> None:
    """aiohttp: an original that returns nothing recorded status_code 200 and success."""
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)
    asyncio.run(hook._wrapped_request("POST", URL, original=nothing, json={}))
    assert [r["status"] for r in _records(writer)] == ["error"]
