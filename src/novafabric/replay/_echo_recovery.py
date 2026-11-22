"""Recover tool results from the next recorded model request (ADR-0306 r3 amendment).

Below the ``forensic`` capture level a tool record keeps no result, only
``io.novafabric.result_digest``: ``sha256`` over the secret-redacted canonical
JSON of the returned value (``_tool_codec.encode_result``). The same result
was sent back to the model in the next request, and the capsule keeps model
requests at every level. This module reads that *echo* with the parser the
D10 echo check uses (``_contract.tool_result_messages``: Chat Completions
``role: tool``, Responses ``function_call_output``, Anthropic ``tool_result``,
Bedrock Converse ``toolResult``), forms a few readings of it (the text, the JSON
it parses to, the content-block list; for a LangChain ``ToolMessage`` result
the envelope), and serves a reading **only** if
``_tool_codec.encode_result(reading).digest`` -- the capture code path, same
canonicalisation, same redaction, same caps -- equals the recorded digest.

**When nothing matches** the record stays unservable, and its reason says
which case it is (``ECHO_RECOVERY_EXT`` marks it, in memory only): no
recorded request carries the call's result -- the run's last tool call, whose
result never went back to a model (:data:`ECHO_NEVER_SENT`); a request carries
it but no reading has the digest (:data:`ECHO_NO_MATCH`); or no recorded model
call holds request messages at all (:data:`ECHO_NO_REQUESTS`). No other capsule
data is a digest-checked source: below ``forensic`` the final graph state is
kept only as a digest (the LangGraph adapter), and the captured stdout holds a
result only if the workload printed it verbatim (ADR-0306 r3b).

**Invariant.** A recovered value is a JSON value whose redacted canonical form
is byte-identical to that of the value the tool returned at capture. It is
therefore the value a ``forensic`` record would have served (that value is
redacted at seal too), and a tool that returned a dict or a list is served
the dict or the list, not the text the model saw. With no digest, no echo,
an ambiguous call id, or no reading that matches, the record is left exactly
as it was (not servable, with its re-capture hint).

Only records whose *sole* obstacle is payload capture being off are
considered; a failure, a non-JSON or nested result, or any other reason keeps
the record unservable. No new data is stored: this reads the capsule only.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from typing import Any

from novafabric.capture import _tool_codec as codec
from novafabric.replay._contract import not_servable_reason, tool_result_messages

__all__ = [
    "ECHO_NEVER_SENT",
    "ECHO_NEVER_SENT_REASON",
    "ECHO_NO_MATCH",
    "ECHO_NO_MATCH_REASON",
    "ECHO_NO_REQUESTS",
    "ECHO_NO_REQUESTS_REASON",
    "ECHO_RECOVERY_EXT",
    "SOURCE_EXT",
    "SOURCE_MODEL_CALL_EXT",
    "SOURCE_MODEL_REQUEST_ECHO",
    "recover_tool_results_from_echo",
]

#: Where a served result came from; absent means the record itself.
SOURCE_EXT = "io.novafabric.result_source"
SOURCE_MODEL_REQUEST_ECHO = "model-request-echo"
#: ``model_call_id`` of the recorded request the result was recovered from.
SOURCE_MODEL_CALL_EXT = "io.novafabric.result_source_model_call_id"

#: Why a payloads-off record was not recovered (set in memory, never written to
#: a capsule): one of the three values below.
ECHO_RECOVERY_EXT = "io.novafabric.echo_recovery"
#: No recorded model request carries this call's result.
ECHO_NEVER_SENT = "result-never-sent"
ECHO_NEVER_SENT_REASON = (
    "result never sent to a model: no recorded model request carries this call's "
    "result (typically the last tool call of the run, or a tool whose result ends "
    "the run), so it cannot be recovered from the model-request echo, and no other "
    "capsule data is a digest-checked source -- capture at NOVA_CAPTURE_LEVEL=forensic "
    "to have it served, or replay with --permissive --allow-unknown-mutation to run "
    "it live"
)
#: A request carries the result, but no reading of it has the recorded digest.
ECHO_NO_MATCH = "echo-digest-mismatch"
ECHO_NO_MATCH_REASON = (
    "a recorded model request carries this call's result, but no reading of it has "
    "the recorded result_digest (the framework changed the result before sending "
    "it), so it is not served -- capture at NOVA_CAPTURE_LEVEL=forensic to have it "
    "served"
)
#: No recorded model call holds request messages to read an echo from.
ECHO_NO_REQUESTS = "no-request-messages"
ECHO_NO_REQUESTS_REASON = (
    "no recorded model call holds request messages, so the result cannot be "
    "recovered from a model-request echo -- capture at NOVA_CAPTURE_LEVEL=forensic "
    "to have it served"
)

#: Extensions holding the model's tool-call id, per framework seam.
_CALL_ID_EXTS = (
    "io.novafabric.langchain_tool_call_id",
    "io.novafabric.openai_agents_call_id",
    "io.novafabric.adk_function_call_id",
)
_RESULT_KIND_EXT = "io.novafabric.langchain_result_kind"
_KIND_TOOL_MESSAGE = "tool_message"


def _payloads_off_reason() -> str:
    from novafabric.capture.hooks._python_tool import PAYLOADS_OFF_REASON

    return PAYLOADS_OFF_REASON


def _call_id(record: dict[str, Any]) -> str | None:
    ext = record.get("extensions")
    if not isinstance(ext, dict):
        return None
    for key in _CALL_ID_EXTS:
        value = ext.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _eligible(record: dict[str, Any]) -> bool:
    ext = record.get("extensions")
    return (
        isinstance(ext, dict)
        and record.get("status", "success") == "success"
        and isinstance(ext.get(codec.RESULT_DIGEST_EXT), str)
        and not_servable_reason(record) == _payloads_off_reason()
    )


def _texts(content: Any) -> Iterator[Any]:
    """Readings of an echo's content: itself, its sole text (or Bedrock ``json``)
    block, and the JSON that text parses to."""
    yield content
    text = content if isinstance(content, str) else None
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        block = content[0]
        if isinstance(block.get("text"), str):
            text = block["text"]
            yield text
        if "json" in block:
            yield block["json"]
    if text is not None:
        try:
            yield json.loads(text)
        except ValueError:
            pass


def _readings(echo: Any, record: dict[str, Any]) -> Iterator[Any]:
    """Candidate values for *record* from one echo (provider shapes unwrapped)."""
    status: list[str] = ["success", "error"]
    content = echo
    if isinstance(echo, dict) and "content" in echo and (
        "status" in echo or "is_error" in echo
    ):
        content = echo.get("content")
        if isinstance(echo.get("status"), str):
            status = [echo["status"]]
        elif "is_error" in echo:
            status = ["error" if echo.get("is_error") else "success"]
    ext = record.get("extensions") or {}
    if ext.get(_RESULT_KIND_EXT) == _KIND_TOOL_MESSAGE:
        for text in _texts(content):
            for state in status:
                for name in (record.get("tool_name"), None):
                    yield {"content": text, "status": state, "name": name, "artifact": None}
        return
    yield from _texts(content)


def _first_echo(
    call_id: str, model_calls: list[dict[str, Any]]
) -> tuple[Any, Any] | None:
    for model in model_calls:
        if not isinstance(model, dict):
            continue
        echoes = tool_result_messages(model.get("gen_ai.request.messages"))
        if echoes and call_id in echoes:
            return echoes[call_id], model.get("model_call_id")
    return None


def _has_request_messages(model_calls: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(m, dict) and m.get("gen_ai.request.messages") for m in model_calls
    )


def _not_recovered(record: dict[str, Any], marker: str, why: str) -> dict[str, Any]:
    """A copy of *record*, still unservable, whose reason also says *why*."""
    ext = dict(record["extensions"])
    ext[ECHO_RECOVERY_EXT] = marker
    ext[codec.NOT_SERVABLE_REASON_EXT] = f"{_payloads_off_reason()}; {why}"
    return {**record, "extensions": ext}


def _recover(
    record: dict[str, Any], model_calls: list[dict[str, Any]], has_requests: bool,
) -> dict[str, Any]:
    call_id = _call_id(record)
    if call_id is None:
        return record
    found = _first_echo(call_id, model_calls)
    if found is None:
        if not has_requests:
            return _not_recovered(record, ECHO_NO_REQUESTS, ECHO_NO_REQUESTS_REASON)
        return _not_recovered(record, ECHO_NEVER_SENT, ECHO_NEVER_SENT_REASON)
    echo, model_call_id = found
    want = record["extensions"][codec.RESULT_DIGEST_EXT]
    for reading in _readings(echo, record):
        try:
            encoded = codec.encode_result(reading, keep_value=False)
        except Exception:  # noqa: BLE001 -- an odd reading is simply not a match
            continue
        if encoded.digest is not None and encoded.digest == want:
            ext = {
                k: v for k, v in record["extensions"].items()
                if k != codec.NOT_SERVABLE_REASON_EXT
            }
            ext[codec.RESULT_CODEC_EXT] = codec.CODEC_JSON
            ext[SOURCE_EXT] = SOURCE_MODEL_REQUEST_ECHO
            if isinstance(model_call_id, str) and model_call_id:
                ext[SOURCE_MODEL_CALL_EXT] = model_call_id
            return {**record, "result": {"value": reading}, "extensions": ext}
    return _not_recovered(record, ECHO_NO_MATCH, ECHO_NO_MATCH_REASON)


def recover_tool_results_from_echo(
    tool_calls: list[dict[str, Any]], model_calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """*tool_calls*, with each payloads-off record whose result the next model
    request echoes -- under exactly the recorded digest -- made servable, and
    each eligible record that was not recovered given a reason naming why
    (:data:`ECHO_RECOVERY_EXT`; still unservable).

    Returns new records for those and the same objects for the rest; never
    modifies its inputs. A call id carried by more than one tool
    record is ambiguous and never recovered.
    """
    counts = Counter(
        _call_id(r) for r in tool_calls if isinstance(r, dict) and _call_id(r) is not None
    )
    has_requests = _has_request_messages(model_calls)
    out: list[dict[str, Any]] = []
    for record in tool_calls:
        if (
            isinstance(record, dict)
            and _eligible(record)
            and counts.get(_call_id(record)) == 1
        ):
            out.append(_recover(record, model_calls, has_requests))
        else:
            out.append(record)
    return out
