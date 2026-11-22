"""Generic wire-level response recording for registry model hosts (ADR-0324, experimental).

The wire hooks (``httpx``/``httpx2``, ``requests``) record the **response** of an HTTP
call to a registry model host, not only its request, when:

* the call is a *logical* model call under ADR-0305 -- no SDK call scope is open (an
  OpenAI/Anthropic SDK call is recorded, and served, by its SDK hook); and
* no dedicated server claims it (Ollama ``/api/chat``/``/api/generate``, ADR-0320; AWS
  Bedrock ``Converse``/``ConverseStream``, ADR-0323); and
* recording is not switched off (``NOVAFABRIC_CAPTURE_WIRE_RESPONSES=off``).

Such a record carries ``io.novafabric.api_surface: "generic-wire"`` (its replay queue),
``io.novafabric.wire.request`` (the match key: method, path and a digest of the canonical
query and body -- never the body itself) and, when the body was recorded,
``io.novafabric.wire.response`` (status, allowlisted headers, the body as UTF-8 text
chunks in read order). When it was not, ``io.novafabric.response_not_recorded`` says
why and mocked replay refuses the record (ADR-0319).

Bodies are stored as decoded text so the finalize-time secret scanner redacts them like
every other value (ADR-0009). Pure helpers and a byte sink only; no import-time IO.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from novafabric.capture.hooks._ollama import RESPONSE_NOT_RECORDED_EXT
from novafabric.capture.hooks._sdk_streams import API_SURFACE_EXT

#: ``API_SURFACE_EXT`` value -- and replay queue -- of a generic wire record.
GENERIC_WIRE_SURFACE = "generic-wire"
#: Reverse-DNS extension keys (additive, optional).
WIRE_REQUEST_EXT = "io.novafabric.wire.request"
WIRE_RESPONSE_EXT = "io.novafabric.wire.response"
#: Format version of both extensions.
WIRE_FORMAT_VERSION = 1

#: Switch: ``off``/``0``/``false``/``no`` records no wire response bodies.
WIRE_RESPONSES_ENV = "NOVAFABRIC_CAPTURE_WIRE_RESPONSES"
_OFF_VALUES = frozenset({"off", "0", "false", "no"})

#: Caps per response (fail closed: past either, the body is dropped, never truncated).
MAX_WIRE_BODY_CHARS = 8 * 1024 * 1024
MAX_WIRE_CHUNKS = 65_536

#: Response headers kept (exact names), and kept prefixes. Everything else -- notably
#: ``set-cookie``, ``authorization``, ``www-authenticate``, ``location``, and the framing
#: headers ``content-encoding``/``content-length``/``transfer-encoding`` -- is dropped.
RESPONSE_HEADER_ALLOWLIST: frozenset[str] = frozenset({
    "content-type", "retry-after", "retry-after-ms", "x-request-id", "request-id",
})
RESPONSE_HEADER_PREFIXES: tuple[str, ...] = ("x-ratelimit-", "ratelimit-")
#: Most headers kept per response.
MAX_WIRE_HEADERS = 64
#: Longest header value kept (characters).
MAX_WIRE_HEADER_VALUE = 1024

#: Query parameters that carry credentials or signatures: never part of the match key.
CREDENTIAL_QUERY_KEYS: frozenset[str] = frozenset({
    "key", "api_key", "apikey", "api-key", "access_token", "token", "sig", "signature",
    "x-goog-api-key",
})
CREDENTIAL_QUERY_PREFIXES: tuple[str, ...] = ("x-amz-",)
#: Top-level JSON request keys that change between runs: never part of the match key.
VOLATILE_BODY_KEYS: frozenset[str] = frozenset({
    "request_id", "requestId", "client_request_id", "timestamp", "nonce",
})


def recording_off_reason() -> str | None:
    """Why wire responses are not recorded in this process, or ``None``. Read at call time."""
    value = os.environ.get(WIRE_RESPONSES_ENV, "").strip().lower()
    if value in _OFF_VALUES:
        return f"wire response recording is off ({WIRE_RESPONSES_ENV}={value})"
    return None


def records_body(gen_ai_system: str, *, in_sdk_call: bool, dedicated: bool) -> bool:
    """Whether a registry-matched wire call is a generic-wire call (ADR-0324 D1/D6)."""
    return bool(gen_ai_system) and gen_ai_system != "unknown" and not (in_sdk_call or dedicated)


# ── the match key ────────────────────────────────────────────────────────────


def _credential_key(name: str) -> bool:
    lowered = name.lower()
    return lowered in CREDENTIAL_QUERY_KEYS or lowered.startswith(CREDENTIAL_QUERY_PREFIXES)


def canonical_query(query: str) -> list[list[str]]:
    """The query's parameters, sorted, without credential parameters."""
    pairs = parse_qsl(query, keep_blank_values=True)
    return sorted([k, v] for k, v in pairs if not _credential_key(k))


def canonical_body(body: bytes | str | None) -> tuple[str, str]:
    """``(kind, canonical text)``: ``json`` (key-sorted, volatile top-level keys removed),
    ``bytes`` (hex of the raw bytes) or ``empty``."""
    if body is None or body == b"" or body == "":
        return "empty", ""
    raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return "bytes", raw.hex()
    if isinstance(parsed, dict):
        parsed = {k: v for k, v in parsed.items() if k not in VOLATILE_BODY_KEYS}
    return "json", json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_key(method: str, url: str, body: bytes | str | None) -> dict[str, Any]:
    """The ``io.novafabric.wire.request`` value: method, path, body kind, digest."""
    parts = urlsplit(url)
    kind, text = canonical_body(body)
    material = json.dumps(
        {"q": canonical_query(parts.query), "b": text}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )
    return {
        "v": WIRE_FORMAT_VERSION,
        "method": str(method or "").upper(),
        "path": parts.path or "/",
        "body": kind,
        "digest": "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
    }


# ── the response ─────────────────────────────────────────────────────────────


def _header_allowed(name: str) -> bool:
    return name in RESPONSE_HEADER_ALLOWLIST or name.startswith(RESPONSE_HEADER_PREFIXES)


def allowed_headers(items: Iterable[Any]) -> list[list[str]]:
    """``[[name, value], ...]`` of the allowlisted headers among *items* (pairs), names
    lower-cased, in order. Anything that is not a pair of strings is skipped."""
    out: list[list[str]] = []
    for item in items:
        try:
            name, value = item
        except (TypeError, ValueError):
            continue
        if isinstance(name, bytes):
            name = name.decode("latin-1")
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        if not (isinstance(name, str) and isinstance(value, str)):
            continue
        lowered = name.strip().lower()
        if _header_allowed(lowered) and len(value) <= MAX_WIRE_HEADER_VALUE:
            out.append([lowered, value])
        if len(out) >= MAX_WIRE_HEADERS:
            break
    return out


def header_pairs(headers: Any) -> list[tuple[Any, Any]]:
    """The (name, value) pairs of an httpx / requests / urllib3 headers object. Never raises."""
    if headers is None:
        return []
    try:
        multi = getattr(headers, "multi_items", None)  # httpx: repeated headers kept
        if callable(multi):
            return list(multi())
        return list(headers.items())
    except Exception:  # noqa: BLE001 -- capture must never fail the workload
        return []


def content_encoding(headers: Any) -> str:
    try:
        value = headers.get("content-encoding", "") if headers is not None else ""
        return str(value or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


class WireBodySink:
    """The body of one response, as UTF-8 text chunks in read order. Never raises.

    ``not_recorded`` is set (and the chunks dropped) on the first reason the body cannot
    be recorded faithfully: a non-identity ``content-encoding`` on bytes not decoded by
    the library, bytes that are not UTF-8, or a cap.
    """

    def __init__(self, *, encoding: str = "") -> None:
        self.chunks: list[str] = []
        self.not_recorded: str | None = None
        self._chars = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        if encoding not in ("", "identity"):
            self._drop(f"the body was read with content-encoding {encoding!r}, "
                       "which capture does not decode")

    def _drop(self, why: str) -> None:
        if self.not_recorded is None:
            self.not_recorded = why
        self.chunks = []

    def add(self, data: Any) -> None:
        if self.not_recorded is not None or not data:
            return
        try:
            text = self._decoder.decode(bytes(data))
        except UnicodeDecodeError:
            self._drop("the response body is not UTF-8 text")
            return
        except Exception:  # noqa: BLE001 -- an odd chunk type loses the body, never the call
            self._drop("the response body could not be copied")
            return
        self._append(text)

    def _append(self, text: str) -> None:
        if not text:
            return
        self._chars += len(text)
        if self._chars > MAX_WIRE_BODY_CHARS:
            self._drop(f"the response body exceeds the {MAX_WIRE_BODY_CHARS}-character cap")
        elif len(self.chunks) >= MAX_WIRE_CHUNKS:
            self._drop(f"the response body exceeds the {MAX_WIRE_CHUNKS}-chunk cap")
        else:
            self.chunks.append(text)

    def close(self) -> None:
        """Flush the decoder: a body that ends inside a multi-byte character is not text."""
        if self.not_recorded is not None:
            return
        try:
            self._append(self._decoder.decode(b"", final=True))
        except UnicodeDecodeError:
            self._drop("the response body is not UTF-8 text")

    # ``_StreamState`` accumulator protocol (``add`` above, ``response`` here).
    def response(self) -> WireBodySink:
        return self


def mark_generic(record: dict[str, Any], method: str, url: str, body: bytes | str | None) -> None:
    """Mark *record* a generic-wire call and attach its match key. Never raises."""
    try:
        ext = record.setdefault("extensions", {})
        ext[API_SURFACE_EXT] = GENERIC_WIRE_SURFACE
        ext[WIRE_REQUEST_EXT] = request_key(method, url, body)
    except Exception:  # noqa: BLE001 -- capture must never fail the workload
        pass


def apply_response(
    record: dict[str, Any], *, status: int, reason: str, headers: Any,
    sink: WireBodySink | None, streamed: bool, complete: bool,
    not_recorded: str | None = None,
) -> None:
    """Attach the recorded response -- or why there is none -- to a generic-wire record."""
    ext = record.setdefault("extensions", {})
    off = recording_off_reason()
    if sink is not None:
        sink.close()
    why = (
        off or not_recorded
        or (sink.not_recorded if sink is not None else "the response body was not read")
    )
    if why is None and not 100 <= status <= 599:
        why = "no HTTP response was received"
    if why is not None:
        ext[RESPONSE_NOT_RECORDED_EXT] = why
        return
    assert sink is not None
    ext[WIRE_RESPONSE_EXT] = {
        "v": WIRE_FORMAT_VERSION,
        "status": status,
        "reason": reason if isinstance(reason, str) else "",
        "headers": allowed_headers(header_pairs(headers)),
        "streamed": streamed,
        "complete": complete,
        "bytes": sum(len(c.encode("utf-8")) for c in sink.chunks),
        "chunks": list(sink.chunks),
    }


def has_wire_response(record: Any) -> bool:
    """A generic-wire record that holds a recorded response (structure only; the replay
    side validates every field again before serving it)."""
    if not isinstance(record, dict):
        return False
    ext = record.get("extensions")
    if not isinstance(ext, dict) or ext.get(API_SURFACE_EXT) != GENERIC_WIRE_SURFACE:
        return False
    response = ext.get(WIRE_RESPONSE_EXT)
    return (
        isinstance(response, dict)
        and isinstance(response.get("chunks"), list)
        and isinstance(ext.get(WIRE_REQUEST_EXT), dict)
    )
