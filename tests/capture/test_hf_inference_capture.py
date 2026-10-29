"""Hugging Face inference capture (HF-02): narrow host classification, honest identity, capture-only.

Nothing here touches the network. The wire hook is driven over ``httpx2.MockTransport`` (``httpx2``
is what ``huggingface_hub`` 2.x and ``openai`` 3.x build on); the tests that need the real
``huggingface_hub`` client skip when it is not installed -- it is not a NovaFabric dependency.

What is pinned, and why:

* only the two OpenAI-compatible Hugging Face routes are model calls; Hub metadata, file
  downloads, look-alike hosts and non-chat routes are not (a bare ``huggingface.co`` match would
  record all of them);
* the record never claims to know the *reported* model or the *served* provider -- the wire hook
  does not read the response and the router selects providers server-side;
* no token reaches any capsule file, from a header, a URL or a prompt;
* mocked replay never serves these records (capture-only), and the support matrix says so.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import _is_ai_api, set_current_recorder
from novafabric.capture.hooks._hf_identity import IDENTITY_EXT, identity_extension, split_selector
from novafabric.capture.hooks._url_registry import load_url_registry
from novafabric.capture.record_roles import (
    RECORD_ROLE_EXT,
    ROLE_LOGICAL,
    ROLE_TRANSPORT,
    count_logical_model_calls,
)
from novafabric.replay._contract import is_replayable_model_call, model_queue_key
from novafabric.replay._support_matrix import ROWS

httpx2 = pytest.importorskip("httpx2")

SPAN = "0" * 16
TOKEN = "hf_" + "Qw3rTy7uIo9pAs5dFg1hJk8lZx2cVb4nMq"  # fake; matches the scanner's HF rule
ROUTER = "https://router.huggingface.co/v1/chat/completions"
ENDPOINT = "https://my-llm.endpoints.huggingface.cloud/v1/chat/completions"
BODY = {"model": "acme/tiny", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 4}


def _ok(request: Any) -> Any:
    return httpx2.Response(200, json={
        "id": "c1", "object": "chat.completion", "created": 1, "model": "acme/tiny-x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    })


@pytest.fixture(autouse=True)
def _clean_global_hook_state() -> Iterator[None]:
    from novafabric.capture.hooks import uninstall_all

    uninstall_all()
    set_current_recorder(None)
    yield
    uninstall_all()
    set_current_recorder(None)


@pytest.fixture
def capsule(tmp_path: Path) -> Iterator[Path]:
    from novafabric.capture.hooks import install_all, uninstall_all

    writer = CapsuleWriter("run-hf", tmp_path)
    writer.open()
    token = install_all(writer, SPAN)
    try:
        yield writer.capsule_dir
    finally:
        uninstall_all(token)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x]


def _calls(capsule: Path) -> list[dict[str, Any]]:
    return _jsonl(capsule / "model-calls.jsonl")


def _client(handler: Any = _ok) -> Any:
    return httpx2.Client(transport=httpx2.MockTransport(handler))


def _post(client: Any, url: str = ROUTER, body: dict[str, Any] | None = None) -> Any:
    return client.post(url, json=body or BODY, headers={"Authorization": f"Bearer {TOKEN}"})


# ── which traffic is a model call ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "system"),
    [
        (ROUTER, "huggingface"),
        ("https://router.huggingface.co/v1/embeddings", "huggingface"),
        (ENDPOINT, "huggingface.endpoint"),
        # Not model calls -- each of these would be recorded by a bare "huggingface.co" match:
        ("https://huggingface.co/api/models/acme/tiny", "unknown"),
        ("https://huggingface.co/datasets/acme/qa/resolve/main/data.parquet", "unknown"),
        ("https://cdn-lfs.huggingface.co/repos/ab/cd/blob", "unknown"),
        ("https://hf.co/acme/tiny", "unknown"),
        # An explicit provider posts under /<provider>/..., not /v1/ -- still the router host:
        ("https://router.huggingface.co/together/v1/chat/completions", "huggingface"),
        ("https://router.huggingface.co/groq/openai/v1/chat/completions", "huggingface"),
        ("https://router.huggingface.co/hf-inference/models/acme/tiny/v1/chat/completions", "huggingface"),
        ("https://router.huggingface.co:443/v1/chat/completions", "huggingface"),
        # Look-alikes and the wrong scheme. The registry match is a *prefix* regex, so a URL that
        # merely contains the real one in a path, query or fragment must not be classified:
        ("https://router.huggingface.co.attacker.example/v1/chat/completions", "unknown"),
        ("https://router.huggingface.co@attacker.example/v1/chat/completions", "unknown"),
        ("https://xendpoints.huggingface.cloud/v1/chat/completions", "unknown"),
        ("http://router.huggingface.co/v1/chat/completions", "unknown"),
        ("http://my-llm.endpoints.huggingface.cloud/v1/chat/completions", "unknown"),
        ("https://attacker.example/?next=https://router.huggingface.co/v1/chat/completions", "unknown"),
        ("https://attacker.example/#https://router.huggingface.co/v1/chat/completions", "unknown"),
        ("https://huggingface.co/api/models?search=https://router.huggingface.co/v1/", "unknown"),
        ("https://attacker.example/r/a.endpoints.huggingface.cloud/v1/x", "unknown"),
    ],
)
def test_only_the_two_hugging_face_inference_hosts_are_classified(
    url: str, system: str
) -> None:
    registry = load_url_registry()
    assert registry.gen_ai_system(url) == system
    assert _is_ai_api(url) is (system != "unknown")


def test_unrelated_hub_traffic_leaves_no_model_call_and_no_ai_egress_event(capsule: Path) -> None:
    client = _client()
    for url in (
        "https://huggingface.co/api/models/acme/tiny",
        "https://cdn-lfs.huggingface.co/x",
        "https://router.huggingface.co.attacker.example/v1/chat/completions",
        "https://attacker.example/?next=https://router.huggingface.co/v1/chat/completions",
        "https://huggingface.co/api/models?search=https://router.huggingface.co/v1/",
    ):
        client.post(url, json=BODY) if "chat" in url else client.get(url)
    assert _calls(capsule) == []
    assert _jsonl(capsule / "network_events.jsonl") == []


# ── what a record says about identity ────────────────────────────────────────


@pytest.mark.parametrize(
    ("sent", "base", "selector", "kind"),
    [
        ("acme/tiny", "acme/tiny", None, None),
        ("acme/tiny:fastest", "acme/tiny", "fastest", "policy"),
        ("acme/tiny:cheapest", "acme/tiny", "cheapest", "policy"),
        ("acme/tiny:preferred", "acme/tiny", "preferred", "policy"),
        ("acme/tiny:groq", "acme/tiny", "groq", "explicit_provider"),
        ("acme/tiny:Not A Provider!", "acme/tiny:Not A Provider!", None, None),
        # An ordinary tag is part of the model string, not a provider choice:
        ("acme/tiny:latest", "acme/tiny:latest", None, None),
        ("llama3:8b", "llama3:8b", None, None),
        ("org/m:fp8", "org/m:fp8", None, None),
        ("https://x.example/m:y", "https://x.example/m:y", None, None),
    ],
)
def test_the_request_selector_is_separated_from_the_model_and_marked_as_requested(
    capsule: Path, sent: str, base: str, selector: str | None, kind: str | None
) -> None:
    _post(_client(), body={**BODY, "model": sent})
    (record,) = _calls(capsule)
    identity = record["extensions"][IDENTITY_EXT]
    assert identity == {
        "requested_model": base,
        "requested_selector": selector,
        "selector_kind": kind,
        "reported_model": None,  # the response was never read
        "served_provider": None,  # the router chooses server-side; nothing observed it
        "identity_source": "request_only",
    }
    assert record["gen_ai.system"] == "huggingface"


def test_a_requested_provider_is_never_reported_as_the_served_provider(capsule: Path) -> None:
    _post(_client(), body={**BODY, "model": "acme/tiny:groq"})
    (record,) = _calls(capsule)
    assert record["extensions"][IDENTITY_EXT]["requested_selector"] == "groq"
    assert record["extensions"][IDENTITY_EXT]["served_provider"] is None


def test_other_providers_records_carry_no_identity_block(capsule: Path) -> None:
    _post(_client(), url="https://api.openai.com/v1/chat/completions")
    (record,) = _calls(capsule)
    assert IDENTITY_EXT not in record["extensions"]


def test_identity_extension_is_none_for_other_systems_and_bounded_for_odd_input() -> None:
    assert identity_extension("openai", "gpt-4o") is None
    assert identity_extension("huggingface", None)["requested_model"] is None  # type: ignore[index]
    long = identity_extension("huggingface", "m" * 5000)
    assert long is not None and len(long["requested_model"]) == 256
    assert split_selector(":groq") == (":groq", None, None)


def test_a_dedicated_endpoint_is_recorded_as_its_own_system(capsule: Path) -> None:
    _post(_client(), url=ENDPOINT, body={**BODY, "model": "tgi"})
    (record,) = _calls(capsule)
    assert record["gen_ai.system"] == "huggingface.endpoint"
    assert record["extensions"][IDENTITY_EXT]["requested_model"] == "tgi"


# ── transport behaviour: errors, streams, cancellation, concurrency ──────────


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_an_error_status_is_recorded_as_an_error_and_the_response_is_untouched(
    capsule: Path, status: int
) -> None:
    response = _post(_client(lambda r: httpx2.Response(status, json={"error": "nope"})))
    assert response.status_code == status and response.json() == {"error": "nope"}
    (record,) = _calls(capsule)
    assert record["status"] == "error"


def test_a_transport_failure_propagates_unchanged_and_is_recorded(capsule: Path) -> None:
    def down(request: Any) -> Any:
        raise httpx2.ConnectError("dns", request=request)

    with pytest.raises(httpx2.ConnectError):
        _post(_client(down))
    assert [r["status"] for r in _calls(capsule)] == ["error"]


def test_a_streamed_response_is_not_read_by_the_hook(capsule: Path) -> None:
    reads: list[int] = []

    def chunks() -> Iterator[bytes]:
        for i in range(3):
            reads.append(i)
            yield f"data: {i}\n\n".encode()

    client = _client(lambda r: httpx2.Response(200, content=chunks(),
                                               headers={"content-type": "text/event-stream"}))
    request = client.build_request("POST", ROUTER, json={**BODY, "stream": True})
    response = client.send(request, stream=True)
    assert reads == [], "the hook consumed the stream before the caller did"
    assert list(response.iter_text()) == ["data: 0\n\n", "data: 1\n\n", "data: 2\n\n"]
    assert reads == [0, 1, 2]  # each chunk produced exactly once
    assert len(_calls(capsule)) == 1  # the call was still recorded, at send time


def test_a_cancelled_async_call_is_recorded_as_failed_and_the_cancellation_propagates(
    capsule: Path,
) -> None:
    async def slow(request: Any) -> Any:
        await asyncio.sleep(30)
        return httpx2.Response(200)

    async def go() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(slow)) as client:
            await asyncio.wait_for(client.post(ROUTER, json=BODY), timeout=0.05)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(go())
    assert [r["status"] for r in _calls(capsule)] == ["error"]


def test_concurrent_threads_each_record_exactly_one_call(capsule: Path) -> None:
    def one(i: int) -> int:
        return _post(_client(), body={**BODY, "model": f"acme/m{i}"}).status_code

    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(one, range(8))) == [200] * 8
    records = _calls(capsule)
    assert sorted(r["gen_ai.request.model"] for r in records) == sorted(f"acme/m{i}" for i in range(8))
    assert len({r["model_call_id"] for r in records}) == 8
    assert count_logical_model_calls(records) == 8


def test_concurrent_async_calls_each_record_exactly_one_call(capsule: Path) -> None:
    async def ahandler(request: Any) -> Any:
        return _ok(request)

    async def go() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(ahandler)) as client:
            await asyncio.gather(*[
                client.post(ROUTER, json={**BODY, "model": f"acme/a{i}"}) for i in range(8)
            ])

    asyncio.run(go())
    assert len(_calls(capsule)) == 8


# ── overlap with an OpenAI-compatible SDK, and no double counting ────────────


def test_an_openai_sdk_pointed_at_the_router_is_still_one_logical_call(capsule: Path) -> None:
    openai = pytest.importorskip("openai")
    client = openai.OpenAI(
        api_key=TOKEN,
        base_url="https://router.huggingface.co/v1",
        http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(_ok)),
    )
    client.chat.completions.create(model="acme/tiny:fastest", messages=BODY["messages"])
    records = _calls(capsule)
    roles = sorted(r["extensions"][RECORD_ROLE_EXT] for r in records)
    assert roles == [ROLE_LOGICAL, ROLE_TRANSPORT]  # SDK = the call; the wire copy = transport
    assert count_logical_model_calls(records) == 1


# ── the token, wherever it appears, never reaches a capsule file ─────────────


def test_no_capsule_file_holds_the_token_from_a_header_a_url_or_a_prompt(tmp_path: Path) -> None:
    from novafabric.adapters._capsule import begin_capture

    cap = begin_capture(framework="hf-test", run_name="token-check", data_dir=tmp_path)
    try:
        with _client() as client:
            client.post(
                f"{ROUTER}?token={TOKEN}",
                json={**BODY, "messages": [{"role": "user", "content": f"my key is {TOKEN}"}]},
                headers={"Authorization": f"Bearer {TOKEN}", "X-Api-Key": TOKEN},
            )
    finally:
        cap.finish()  # the real finalisation path, including the secret scan
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files, "the capsule was not written"
    assert all(TOKEN.encode() not in p.read_bytes() for p in files)


def test_hugging_face_records_are_no_more_and_no_less_permissive_than_openais(capsule: Path) -> None:
    """Content handling is the capsule-wide policy, not something this feature decides."""
    client = _client()
    _post(client, url=ROUTER)
    _post(client, url="https://api.openai.com/v1/chat/completions")
    hf, oa = _calls(capsule)
    assert hf["gen_ai.request.messages"] == oa["gen_ai.request.messages"]
    assert hf["gen_ai.usage.input_tokens"] == oa["gen_ai.usage.input_tokens"] == 0


# ── replay: capture only, said in code and in the matrix ─────────────────────


def test_a_hugging_face_record_is_never_a_replayable_model_call(capsule: Path) -> None:
    _post(_client())
    (record,) = _calls(capsule)
    assert model_queue_key(record) is None
    assert is_replayable_model_call(record) is False


def test_the_replay_support_matrix_states_hugging_face_is_capture_only() -> None:
    row = next(r for r in ROWS if "Hugging Face" in r.surface and "InferenceClient" in r.surface)
    assert row.replay == "not intercepted" and row.status == "capture only"
    assert not row.patches and not row.servers
    assert "request" in row.capture.lower() and "response" in row.capture.lower()


# ── the real client (skips when huggingface_hub is not installed) ────────────


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    hub = pytest.importorskip("huggingface_hub")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    yield hub
    from huggingface_hub.utils._http import default_async_client_factory, default_client_factory

    hub.set_client_factory(default_client_factory)
    hub.set_async_client_factory(default_async_client_factory)
    hub.close_session()


def test_real_inference_client_sync_chat_is_recorded_once_with_honest_identity(
    capsule: Path, hub: Any
) -> None:
    seen: list[str] = []

    def handler(request: Any) -> Any:
        seen.append(str(request.url))
        return _ok(request)

    hub.set_client_factory(lambda: httpx2.Client(transport=httpx2.MockTransport(handler)))
    out = hub.InferenceClient(token=TOKEN).chat_completion(
        model="acme/tiny:cheapest", messages=BODY["messages"], max_tokens=4
    )
    assert out.choices[0].message.content == "hi"  # the caller's result is untouched
    records = _calls(capsule)
    assert [r["gen_ai.system"] for r in records] == ["huggingface"]
    identity = records[0]["extensions"][IDENTITY_EXT]
    assert (identity["requested_model"], identity["selector_kind"]) == ("acme/tiny", "policy")
    assert identity["reported_model"] is None and identity["served_provider"] is None
    assert all(TOKEN not in json.dumps(r) for r in records)


def test_real_inference_client_stream_is_not_drained_by_capture(capsule: Path, hub: Any) -> None:
    def handler(request: Any) -> Any:
        chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "acme/tiny",
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": "x"},
                              "finish_reason": None}]}
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    hub.set_client_factory(lambda: httpx2.Client(transport=httpx2.MockTransport(handler)))
    stream = hub.InferenceClient(token=TOKEN).chat_completion(
        model="acme/tiny", messages=BODY["messages"], stream=True, max_tokens=4
    )
    assert [c.choices[0].delta.content for c in stream] == ["x"]
    assert len(_calls(capsule)) == 1


def test_real_async_inference_client_is_recorded(capsule: Path, hub: Any) -> None:
    async def ahandler(request: Any) -> Any:
        return _ok(request)

    hub.set_async_client_factory(lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(ahandler)))

    async def go() -> Any:
        return await hub.AsyncInferenceClient(token=TOKEN).chat_completion(
            model="acme/tiny", messages=BODY["messages"], max_tokens=4
        )

    out = asyncio.run(go())
    assert out.choices[0].message.content == "hi"
    assert [r["gen_ai.system"] for r in _calls(capsule)] == ["huggingface"]


def test_real_hub_metadata_calls_are_not_model_calls(capsule: Path, hub: Any) -> None:
    def handler(request: Any) -> Any:
        return httpx2.Response(200, json={"id": "acme/tiny", "sha": "a" * 40, "siblings": []})

    hub.set_client_factory(lambda: httpx2.Client(transport=httpx2.MockTransport(handler)))
    hub.HfApi().model_info("acme/tiny", token=False)
    assert _calls(capsule) == []


def test_a_custom_base_url_is_not_captured_unless_the_registry_names_it(
    capsule: Path, hub: Any
) -> None:
    """A known limitation, pinned so it cannot become an accidental broad match."""
    hub.set_client_factory(lambda: httpx2.Client(transport=httpx2.MockTransport(_ok)))
    hub.InferenceClient(base_url="https://llm.internal.example/v1", token=TOKEN).chat_completion(
        messages=BODY["messages"], max_tokens=4
    )
    assert _calls(capsule) == []


# ── the real client's explicit-provider routes (review finding: they were all missed) ─────────


PROVIDERS = ("hf-inference", "together", "groq", "novita", "cerebras", "fireworks-ai", "cohere", "zai-org")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_explicit_provider_route_of_the_real_client_is_captured(
    capsule: Path, hub: Any, provider: str
) -> None:
    # The client caches this lookup per model for the process, so every provider is served at once.
    mapping = {
        name: {"status": "live", "providerId": f"acme/tiny-{name}", "task": "conversational"}
        for name in PROVIDERS
    }

    def handler(request: Any) -> Any:
        if "/api/models/" in str(request.url):  # the client's own provider lookup
            return httpx2.Response(200, json={
                "id": "acme/tiny", "sha": "a" * 40, "pipeline_tag": "text-generation",
                "tags": ["conversational"], "inferenceProviderMapping": mapping,
            })
        return _ok(request)

    hub.set_client_factory(lambda: httpx2.Client(transport=httpx2.MockTransport(handler)))
    hub.InferenceClient(provider=provider, api_key=TOKEN).chat_completion(
        model="acme/tiny", messages=[{"role": "user", "content": "hi"}], max_tokens=4
    )
    (record,) = _calls(capsule)  # exactly one model call; the Hub lookup is not one
    assert record["gen_ai.system"] == "huggingface"
    assert "/api/models/" not in record["endpoint"] and "router.huggingface.co/" in record["endpoint"]


def test_a_dedicated_endpoint_has_no_selector_even_when_the_model_string_looks_like_one() -> None:
    identity = identity_extension("huggingface.endpoint", "acme/tiny:groq")
    assert identity is not None
    assert identity["requested_model"] == "acme/tiny:groq"
    assert identity["requested_selector"] is None and identity["selector_kind"] is None
