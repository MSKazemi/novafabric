"""The mocked-replay contract (ADR-0300).

One place that decides, for both the engine (counting) and the in-process
dispatchers (serving):

* which recorded model calls a dispatcher can serve, and on which surface;
* which recorded tool calls sit on an intercepted surface;
* how a replayed tool call is matched to a recorded one -- one-to-one, never
  reusing a record;
* how the dispatchers' event log is summarised into the counters a replay
  result reports.

Pure functions and small dataclasses only; no import-time IO.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from novafabric.capture import _tool_codec
from novafabric.capture._tool_codec import normalized_arg_hash as normalized_arg_hash
from novafabric.capture.hooks._sdk_streams import (
    ANTHROPIC_MESSAGES_SURFACE,
    API_SURFACE_EXT,
    OPENAI_CHAT_SURFACE,
    OPENAI_RESPONSES_SURFACE,
)
from novafabric.capture.record_roles import (
    ROLE_LOGICAL,
    classify_model_calls,
    is_transport_record,
)
from novafabric.replay._model_errors import is_recorded_model_error

#: Model API surfaces mocked replay serves from the capsule, keyed by replay
#: queue (``model_queue_key``). Each is served sync and async, with and without
#: ``stream=True`` (ADR-0304). Everything else is refused (strict) or runs live
#: (permissive) -- see ``UNSUPPORTED_MODEL_SURFACES`` in ``_dispatcher``.
MODEL_SURFACES: dict[str, str] = {
    "openai": "openai.chat.completions.create",
    "openai.responses": "openai.responses.create",
    "anthropic": "anthropic.messages.create",
}

#: The provider (``gen_ai.system``) behind each replay queue.
QUEUE_PROVIDER: dict[str, str] = {
    "openai": "openai",
    "openai.responses": "openai",
    "anthropic": "anthropic",
}

#: How each served model surface may be called during a mocked replay.
MODEL_CALL_MODES = "sync and async; stream=True and non-streaming"

#: The tool surfaces mocked replay intercepts (ADR-0300, ADR-0306).
TOOL_SURFACE_MCP = "mcp.ClientSession.call_tool"
TOOL_SURFACE_PYTHON = "novafabric.capture.record.tool"
TOOL_SURFACES: tuple[str, ...] = (TOOL_SURFACE_MCP, TOOL_SURFACE_PYTHON)

DivergencePolicy = Literal["fail", "warn"]

#: Most divergences listed individually in a result; the rest are counted.
MAX_LISTED_DIVERGENCES = 50

#: Most distinct ``host:port`` network destinations listed in a result.
MAX_LISTED_NETWORK_DESTINATIONS = 20


# ── model calls ──────────────────────────────────────────────────────────────


def model_queue_key(record: dict[str, Any]) -> str | None:
    """The replay queue a recorded model call belongs to, or ``None``.

    Chat Completions and Messages records carry no surface marker (every
    pre-ADR-0304 capsule); Responses API records carry
    ``extensions["io.novafabric.api_surface"] = "openai.responses"``. A record
    marked with any other surface is not servable.
    """
    system = record.get("gen_ai.system")
    if system not in ("openai", "anthropic"):
        return None
    surface = _surface_marker(record)
    if surface is None:
        return str(system)
    return _MARKER_QUEUE.get((str(system), surface))


#: (gen_ai.system, API_SURFACE_EXT value) -> replay queue.
_MARKER_QUEUE: dict[tuple[str, str], str] = {
    ("openai", OPENAI_CHAT_SURFACE): "openai",
    ("openai", OPENAI_RESPONSES_SURFACE): "openai.responses",
    ("anthropic", ANTHROPIC_MESSAGES_SURFACE): "anthropic",
}


def _surface_marker(record: dict[str, Any]) -> str | None:
    ext = record.get("extensions")
    surface = ext.get(API_SURFACE_EXT) if isinstance(ext, dict) else None
    return surface if isinstance(surface, str) else None


def records_async_and_streamed_calls(model_calls: list[dict[str, Any]]) -> bool:
    """Whether the capsule was captured by hooks that record async and streamed
    calls (ADR-0304).

    Those hooks mark every record with its API surface. A capsule with servable
    records and no marker was captured before: its async and streamed calls
    were never recorded, so serving one from the queue would hand it a record
    that belonged to another call. Mocked replay refuses them for such a capsule,
    exactly as before ADR-0304. A capsule with no servable record is not legacy:
    any call simply finds its queue empty.
    """
    servable = served_model_records(model_calls)
    return not servable or any(_surface_marker(r) is not None for r in servable)


def is_replayable_model_call(record: dict[str, Any]) -> bool:
    """A record a dispatcher can serve: an intercepted surface, a successful
    call, and at least one recorded choice.

    This excludes, by construction, the wire-level duplicate that the
    ``httpx`` hook writes beside every SDK-level record (it carries
    ``gen_ai.response.choices: []``, and since ADR-0305 is marked
    ``extensions["io.novafabric.record_role"]: "transport"``), error records,
    and calls whose response was never recorded (e.g. captured before ADR-0304
    on an async or streamed path). A future serving rule that relaxes the
    status or choices test must keep the transport test: a transport record is
    an HTTP attempt, never a call to serve.
    """
    if is_transport_record(record):
        return False
    if model_queue_key(record) is None:
        return False
    if record.get("status", "success") != "success":
        return False
    choices = record.get("gen_ai.response.choices")
    return isinstance(choices, list) and len(choices) > 0


def is_replayable_model_error(record: dict[str, Any]) -> bool:
    """A recorded model call that FAILED and holds a queue position (issue #16).

    An intercepted surface, not a transport record, a non-success status and an
    ``error`` block -- the SDK hook's record of a call that raised. Mocked replay
    raises the recorded exception at that position (``_model_errors``), or fails
    closed when it cannot rebuild it. Wire records never carry an ``error``
    block, so an HTTP attempt the SDK retried is never an error position; whole-
    capsule classification (:func:`served_model_records`) additionally drops
    records ADR-0305's legacy fallback identifies as transport.
    """
    if is_transport_record(record):
        return False
    if model_queue_key(record) is None:
        return False
    return is_recorded_model_error(record)


def served_model_records(model_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records that hold a replay queue position, in recorded order.

    Successful records with a response (:func:`is_replayable_model_call`) and
    failed SDK calls (:func:`is_replayable_model_error`), restricted to records
    that are *logical* model calls under ADR-0305 -- by marker, or by the legacy
    fallback for a capsule captured before the markers. A transport record (an
    HTTP attempt, retries included) is never served, not even an orphaned one
    that a count promotes.
    """
    roles = classify_model_calls(model_calls)
    return [
        record
        for record, role in zip(model_calls, roles)
        if isinstance(record, dict)
        and role.role == ROLE_LOGICAL
        and (is_replayable_model_call(record) or is_replayable_model_error(record))
    ]


def model_queues(model_calls: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per-surface queues of served records (responses and errors), in recorded order."""
    queues: dict[str, list[dict[str, Any]]] = {q: [] for q in MODEL_SURFACES}
    for record in served_model_records(model_calls):
        queues[str(model_queue_key(record))].append(record)
    return queues


def recorded_provider_order(model_calls: list[dict[str, Any]]) -> list[str]:
    """The queue of each served record, in recorded order."""
    return [str(model_queue_key(r)) for r in served_model_records(model_calls)]


# ── tool calls ───────────────────────────────────────────────────────────────


def _mcp_method(record: dict[str, Any]) -> str:
    mcp = record.get("mcp")
    if isinstance(mcp, dict) and isinstance(mcp.get("method"), str):
        return str(mcp["method"])
    return "tools/call"


def _is_proxy_record(record: dict[str, Any]) -> bool:
    ext = record.get("extensions")
    return isinstance(ext, dict) and ext.get("io.novafabric.capture_method") == "proxy"


def tool_surface(record: dict[str, Any]) -> str | None:
    """The intercepted surface a recorded tool call belongs to, or ``None``.

    * ``TOOL_SURFACE_MCP``: an MCP ``tools/call`` with a name (ADR-0300);
    * ``TOOL_SURFACE_PYTHON``: a ``record.tool`` boundary -- ``transport:
      "python"`` **and** the ``io.novafabric.tool_surface: "python.function"``
      marker (ADR-0306). Absent on every older record, which keeps its meaning.

    A record is matchable only on the surface it was recorded on.
    """
    if not isinstance(record, dict):
        return None
    name = record.get("tool_name")
    if not isinstance(name, str) or not name:
        return None
    transport = record.get("transport")
    if transport == "mcp" and _mcp_method(record) == "tools/call":
        return TOOL_SURFACE_MCP
    if transport == "python":
        ext = record.get("extensions")
        if (
            isinstance(ext, dict)
            and ext.get(_tool_codec.TOOL_SURFACE_EXT) == _tool_codec.TOOL_SURFACE_PYTHON_FUNCTION
        ):
            return TOOL_SURFACE_PYTHON
    return None


def is_interceptable_tool_call(record: dict[str, Any]) -> bool:
    """Recorded on an intercepted surface (:func:`tool_surface`)."""
    return tool_surface(record) is not None


def not_servable_reason(record: dict[str, Any]) -> str | None:
    """Why an intercepted record cannot be served, or ``None`` if it can.

    MCP records are always servable (ADR-0300). A python-surface record is
    servable only when capture marked its codec ``json-v1`` (ADR-0306 D3).
    """
    if tool_surface(record) != TOOL_SURFACE_PYTHON:
        return None
    ext = record.get("extensions")
    ext = ext if isinstance(ext, dict) else {}
    if ext.get(_tool_codec.RESULT_CODEC_EXT) == _tool_codec.CODEC_JSON:
        if record.get("status", "success") == "success" and not isinstance(
            record.get("result"), dict
        ):
            return "the record holds no result value"
        return None
    reason = ext.get(_tool_codec.NOT_SERVABLE_REASON_EXT)
    if _only_nested(record, ext, reason):
        return None
    return str(reason) if reason else "the record was not marked servable at capture"


def _only_nested(record: dict[str, Any], ext: dict[str, Any], reason: Any) -> bool:
    """A slice-1 record whose ONLY obstacle was nesting (ADR-0306 slice 4).

    Slice-1 capture marked a boundary with nested records not servable, with
    exactly ``_tool_codec.legacy_nested_reason(n)`` as its reason and its value
    kept. Nested boundaries are served now (their nested records are consumed as
    covered), so such a record is servable without a re-capture. Any other
    reason in the text -- payloads off, a non-JSON result, the size cap, a
    non-canonical argument -- keeps it unservable.
    """
    nested = ext.get(_tool_codec.NESTED_RECORDS_EXT)
    if type(nested) is not int or nested <= 0:
        return False
    if reason != _tool_codec.legacy_nested_reason(nested):
        return False
    if record.get("status", "success") == "success":
        result = record.get("result")
        return isinstance(result, dict) and "value" in result
    return True


def nested_within(record: dict[str, Any]) -> str | None:
    """The ``tool_call_id`` of the ``record.tool`` boundary *record* was written
    inside (``io.novafabric.within_tool_call_id``, ADR-0306 D3), or ``None``."""
    ext = record.get("extensions") if isinstance(record, dict) else None
    within = ext.get(_tool_codec.WITHIN_TOOL_CALL_EXT) if isinstance(ext, dict) else None
    return within if isinstance(within, str) and within else None


def nested_boundary_ids(boundary_id: str, tool_calls: list[dict[str, Any]]) -> set[str]:
    """*boundary_id* and every boundary recorded inside it, transitively.

    A decorated function may call another decorated function: the inner
    boundary's own nested records carry the inner id, so covering the outer
    boundary must cover them too.
    """
    ids = {boundary_id}
    children: dict[str, list[str]] = {}
    for rec in tool_calls:
        within = nested_within(rec)
        tool_call_id = rec.get("tool_call_id") if isinstance(rec, dict) else None
        if within and isinstance(tool_call_id, str) and tool_call_id:
            children.setdefault(within, []).append(tool_call_id)
    frontier = [boundary_id]
    while frontier:
        for child in children.get(frontier.pop(), []):
            if child not in ids:
                ids.add(child)
                frontier.append(child)
    return ids


def is_servable_tool_record(record: dict[str, Any]) -> bool:
    """On an intercepted surface and servable: what ``tool_calls_available`` counts."""
    return is_interceptable_tool_call(record) and not_servable_reason(record) is None


def _stored_arguments_digest(record: dict[str, Any]) -> str | None:
    """The payloads-off python record's stored digest, else ``None``."""
    if tool_surface(record) != TOOL_SURFACE_PYTHON:
        return None
    ext = record.get("extensions")
    digest = ext.get(_tool_codec.ARGUMENTS_DIGEST_EXT) if isinstance(ext, dict) else None
    return digest if isinstance(digest, str) and digest else None


def _record_arguments(record: dict[str, Any]) -> Any:
    arguments = record.get("arguments")
    if tool_surface(record) == TOOL_SURFACE_PYTHON:
        return arguments or {}
    return arguments


def record_arguments_digest(record: dict[str, Any]) -> str:
    """The argument digest a replayed call is matched against: redact-then-hash.

    Every surface hashes a record's arguments **after** ADR-0009 secret
    redaction (``_tool_codec.redacted_arguments_digest``), exactly as the live
    call's arguments are hashed (ADR-0306 D12.3), so an argument the capsule
    scanner redacted at seal still matches -- the python surface since slice 1,
    MCP since slice 4. Redaction is idempotent, so a sealed and an unsealed
    capsule hash alike. A python-surface record captured with payload capture
    off holds no arguments, only this digest (``io.novafabric.arguments_digest``).
    """
    stored = _stored_arguments_digest(record)
    if stored is not None:
        return stored
    return _tool_codec.redacted_arguments_digest(_record_arguments(record))


def record_raw_arguments_digest(record: dict[str, Any]) -> str | None:
    """ADR-0300's raw digest of the stored arguments, for the matcher's exact tier.

    ``None`` when the record holds no arguments (payloads off). Computed in
    memory at replay only, never written: the dual-hash fallback that keeps a
    capsule matching exactly as it did before redact-then-hash (see
    :class:`ToolCallMatcher`).
    """
    if _stored_arguments_digest(record) is not None:
        return None
    return normalized_arg_hash(_record_arguments(record))


def _nests(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    try:
        return bool(
            str(outer["started_at"]) <= str(inner["started_at"])
            and str(inner["finished_at"]) <= str(outer["finished_at"])
        )
    except KeyError:
        return False


def interceptable_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recorded tool calls a dispatcher can serve, in recorded order.

    When one call was captured twice -- by the in-process MCP hook *and* by
    ``nova mcp-proxy`` on the wire -- the proxy record (same name and arguments,
    time interval nested inside the hook record's) is dropped, so one replayed
    call is never expected to consume two records.
    """
    candidates = [r for r in tool_calls if is_interceptable_tool_call(r)]
    hook_records = [r for r in candidates if not _is_proxy_record(r)]
    absorbed: set[int] = set()
    dropped: set[int] = set()
    for idx, rec in enumerate(candidates):
        if not _is_proxy_record(rec):
            continue
        signature = (rec.get("tool_name"), normalized_arg_hash(rec.get("arguments")))
        for h_idx, hook in enumerate(hook_records):
            if h_idx in absorbed:
                continue
            if (
                (hook.get("tool_name"), normalized_arg_hash(hook.get("arguments")))
                == signature
                and _nests(hook, rec)
            ):
                absorbed.add(h_idx)
                dropped.add(idx)
                break
    return [r for i, r in enumerate(candidates) if i not in dropped]


MatchHow = Literal["id", "signature", "redacted-signature"]


@dataclass
class ToolMatch:
    """Outcome of matching one replayed tool call.

    ``how`` is ``"redacted-signature"`` when the match needed redact-then-hash:
    the live arguments held a detected secret and no record held the same raw
    arguments, so recorded order chose among records whose secrets redact alike.
    """

    record: dict[str, Any] | None
    how: MatchHow | None = None
    reason: str | None = None


class ToolCallMatcher:
    """One-to-one matching of replayed tool calls to recorded ones.

    Order of preference:

    1. exact ``tool_call_id`` (when the surface carries one) with the same name;
    2. tool name + argument hash, earliest unconsumed record first -- so
       repeated identical calls consume distinct records in recorded order.
       Two hashes, in this order (the dual-hash fallback, ADR-0306 D12.3):

       a. the **raw** hash (ADR-0300 D6): the record holds exactly these
          arguments. Every call an older replay matched still matches, and to
          the same record;
       b. the **redact-then-hash** digest (:func:`record_arguments_digest`):
          the arguments are equal once ADR-0009 secrets are masked, so a call
          whose secret the capsule scanner redacted at seal still matches.
          Two different secrets caught by one rule collapse to one value here,
          and recorded order decides between them (``how`` says so).
    3. otherwise **unmatched**, with a reason. A record is never served twice:
       this is the dict-keyed-by-signature bug class ``nova diff`` once had.

    Indexed by ``(name, digest)`` -> queue of unconsumed record indices, so
    tier 2 is amortised O(1) per call instead of a scan of the capsule
    (ADR-0306 D9). Thread-safe: decorated python tools may run on executor
    threads (ADR-0306 D6.4). Raw digests live in memory only.
    """

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = list(records)
        self._hashes = [record_arguments_digest(r) for r in self._records]
        self._raw_hashes = [record_raw_arguments_digest(r) for r in self._records]
        self._consumed = [False] * len(self._records)
        self._consumed_count = 0
        self._lock = threading.Lock()
        self._index: dict[tuple[Any, str], deque[int]] = {}
        self._raw_index: dict[tuple[Any, str], deque[int]] = {}
        self._signatures_by_name: dict[Any, set[str]] = {}
        for idx, (rec, digest) in enumerate(zip(self._records, self._hashes)):
            name = rec.get("tool_name")
            self._index.setdefault((name, digest), deque()).append(idx)
            self._signatures_by_name.setdefault(name, set()).add(digest)
            raw = self._raw_hashes[idx]
            if raw is not None:
                self._raw_index.setdefault((name, raw), deque()).append(idx)

    def __len__(self) -> int:
        return len(self._records)

    @property
    def consumed_count(self) -> int:
        return self._consumed_count

    def unconsumed(self) -> list[dict[str, Any]]:
        return [r for r, used in zip(self._records, self._consumed) if not used]

    def _take(self, idx: int, how: MatchHow) -> ToolMatch:
        self._consumed[idx] = True
        self._consumed_count += 1
        return ToolMatch(record=self._records[idx], how=how)

    def _first_unconsumed(self, queue: deque[int] | None) -> int | None:
        while queue:
            idx = queue.popleft()
            if not self._consumed[idx]:  # another tier may have taken it
                return idx
        return None

    def match(
        self, tool_call_id: str | None, tool_name: str, arguments: Any
    ) -> ToolMatch:
        return self.match_digest(
            tool_call_id, tool_name,
            _tool_codec.redacted_arguments_digest(arguments),
            raw_digest=normalized_arg_hash(arguments),
        )

    def cover(self, boundary_ids: set[str]) -> list[dict[str, Any]]:
        """Consume, without serving, every unconsumed record written inside one of
        *boundary_ids* (ADR-0306 slice 4, nested coverage); return them.

        A served ``record.tool`` boundary never runs its body, so the calls it
        made at capture are never requested: they are *covered* by the served
        result, not left over. Each record is covered at most once.
        """
        covered: list[dict[str, Any]] = []
        with self._lock:
            for idx, rec in enumerate(self._records):
                if not self._consumed[idx] and nested_within(rec) in boundary_ids:
                    self._consumed[idx] = True
                    self._consumed_count += 1
                    covered.append(rec)
        return covered

    def match_digest(
        self, tool_call_id: str | None, tool_name: str, digest: str,
        *, raw_digest: str | None = None,
    ) -> ToolMatch:
        """As :meth:`match`, for a caller that already hashed its arguments.

        *digest* is the redact-then-hash digest (tier 2b); *raw_digest*, when
        given, is tried first (tier 2a).
        """
        with self._lock:
            if tool_call_id:
                for idx, rec in enumerate(self._records):
                    if self._consumed[idx] or rec.get("tool_call_id") != tool_call_id:
                        continue
                    if rec.get("tool_name") == tool_name:
                        return self._take(idx, "id")
                    return ToolMatch(
                        record=None,
                        reason=(
                            f"tool_call_id {tool_call_id!r} was recorded for "
                            f"{rec.get('tool_name')!r}, not {tool_name!r}"
                        ),
                    )
            if raw_digest is not None:
                found = self._first_unconsumed(self._raw_index.get((tool_name, raw_digest)))
                if found is not None:
                    return self._take(found, "signature")
            found = self._first_unconsumed(self._index.get((tool_name, digest)))
            if found is not None:
                # Redaction changed nothing in the live arguments, or the record
                # holds them raw: no secret was collapsed to reach this record.
                exact = raw_digest is None or raw_digest in (digest, self._raw_hashes[found])
                return self._take(found, "signature" if exact else "redacted-signature")
            signatures = self._signatures_by_name.get(tool_name)
            if signatures and digest in signatures:
                reason = "every recorded call with these arguments was already consumed"
            elif signatures:
                reason = "the tool was recorded, but not with these arguments"
            else:
                reason = "no recorded call to this tool"
            return ToolMatch(record=None, reason=reason)


# ── tool-result echo check (ADR-0306 D10, slice 4) ───────────────────────────

#: Kind of a reported echo mismatch. Report-only (owner Q10): it is listed under
#: ``replay_contract.tool_result_echo``, never in ``divergences``, and never fails
#: a replay.
TOOL_RESULT_ECHO_MISMATCH = "tool_result_echo_mismatch"
TOOL_RESULT_ECHO_POLICY = "report-only"

#: ``not_checked`` reasons (stable strings, counted in the result).
ECHO_NOT_RECORDED = "the capsule recorded no request messages for this model call"
ECHO_NO_COUNTERPART = "no tool result with this id was recorded at this position"
#: ``mismatched`` reasons.
ECHO_DIFFERS = "the result the replay sent back differs from the recorded one"
ECHO_NOT_SENT = "recorded at this position, but the replay sent no result for it"


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def tool_result_messages(messages: Any) -> dict[str, Any] | None:
    """``tool_call_id`` -> the tool result a model request sends back (D10, fact 6).

    Reads the three shapes the served surfaces use, from dicts or SDK objects:

    * Chat Completions: a ``role: "tool"`` message -> its ``content``;
    * Anthropic Messages: a ``tool_result`` block in a ``user`` message ->
      ``{"content", "is_error"}``, keyed by ``tool_use_id``;
    * Responses API: a ``function_call_output`` input item -> its ``output``.

    ``None`` when *messages* is not a non-empty list: a request with no message
    list (or a record that kept none) has nothing to compare, which is never
    the same as "matched". The first result per id wins.
    """
    if not isinstance(messages, (list, tuple)) or not messages:
        return None
    out: dict[str, Any] = {}

    def put(call_id: Any, value: Any) -> None:
        if isinstance(call_id, str) and call_id and call_id not in out:
            out[call_id] = value

    for item in messages:
        if _field(item, "role") == "tool":
            put(_field(item, "tool_call_id"), _field(item, "content"))
        elif _field(item, "type") == "function_call_output":
            put(_field(item, "call_id"), _field(item, "output"))
        elif _field(item, "role") == "user":
            content = _field(item, "content")
            if isinstance(content, (list, tuple)):
                for block in content:
                    if _field(block, "type") == "tool_result":
                        put(_field(block, "tool_use_id"), {
                            "content": _field(block, "content"),
                            "is_error": bool(_field(block, "is_error")),
                        })
    return out


def request_messages(kwargs: dict[str, Any]) -> Any:
    """The message list of a replayed ``create`` call (``input`` for Responses)."""
    if "messages" in kwargs:
        return kwargs.get("messages")
    return kwargs.get("input")


# ── event log (written in the replayed process, read by the engine) ──────────


class ReplayEventLog:
    """Append-only JSON-lines log of what the dispatchers did.

    Written line by line, so it survives a crash of the replayed process; each
    line carries the writer's pid, so a command that starts several Python
    interpreters is visible. Writing never raises into the workload.
    """

    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self._path = Path(path) if path else None

    def emit(self, event: str, **fields: Any) -> None:
        if self._path is None:
            return
        line = json.dumps({"event": event, "pid": os.getpid(), **fields}, default=str)
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


# ── summary ──────────────────────────────────────────────────────────────────


@dataclass
class ReplayContractReport:
    """What a mocked replay actually served, refused and left over."""

    divergence_policy: str
    substitute_tools: bool
    interception_surfaces: list[str] = field(default_factory=list)
    dispatcher_installed: bool = False
    model_calls_recorded: int = 0
    model_calls_available: int = 0
    model_calls_mocked: int = 0
    model_calls_unmatched: int = 0
    model_calls_live: int = 0
    model_calls_unconsumed: int = 0
    model_errors_replayed: int = 0
    tool_calls_recorded: int = 0
    tool_calls_available: int = 0
    tool_calls_not_interceptable: int = 0
    tool_calls_mocked: int = 0
    tool_calls_live: int = 0
    tool_calls_unmatched: int = 0
    tool_calls_unconsumed: int = 0
    tool_calls_refused: int = 0
    tool_calls_by_surface: dict[str, dict[str, int]] = field(default_factory=dict)
    network_observed: bool = False
    network_connections_live: int = 0
    network_connections_capped: bool = False
    network_destinations: list[str] = field(default_factory=list)
    divergences: list[dict[str, Any]] = field(default_factory=list)
    #: ``replay.yaml`` overrides and whether this replay honours each
    #: (ADR-0306 D11); written only when the capsule has overrides.
    tool_overrides: list[dict[str, Any]] = field(default_factory=list)
    #: Records consumed as covered by a served nested ``record.tool`` boundary
    #: (ADR-0306 slice 4): never requested, because the boundary's body never ran.
    model_calls_covered: int = 0
    tool_calls_covered: int = 0
    #: The D10 echo check, report-only (ADR-0306 slice 4, owner Q10).
    echo_matched: int = 0
    echo_not_checked: dict[str, int] = field(default_factory=dict)
    echo_mismatches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def queues_fully_consumed(self) -> bool:
        return self.model_calls_unconsumed == 0 and (
            not self.substitute_tools or self.tool_calls_unconsumed == 0
        )

    @property
    def diverged(self) -> bool:
        return bool(self.divergences)

    @property
    def divergence_reason(self) -> str | None:
        if not self.divergences:
            return None
        first = self.divergences[0]
        reason = f"{first.get('kind', 'divergence')}: {first.get('message', '')}".strip()
        more = len(self.divergences) - 1
        if more > 0:
            reason += f" (+{more} more divergence{'s' if more > 1 else ''})"
        return reason

    def tool_result_echo(self) -> dict[str, Any]:
        """The D10 echo check as the result reports it (report-only)."""
        listed = self.echo_mismatches[:MAX_LISTED_DIVERGENCES]
        out: dict[str, Any] = {
            "policy": TOOL_RESULT_ECHO_POLICY,
            "matched": self.echo_matched,
            "mismatched": len(self.echo_mismatches),
            "not_checked": sum(self.echo_not_checked.values()),
            "not_checked_reasons": dict(self.echo_not_checked),
            "mismatches": [dict(m) for m in listed],
        }
        if len(self.echo_mismatches) > len(listed):
            out["mismatches_not_listed"] = len(self.echo_mismatches) - len(listed)
        return out

    def as_dict(self) -> dict[str, Any]:
        listed = self.divergences[:MAX_LISTED_DIVERGENCES]
        out: dict[str, Any] = {
            "divergence_policy": self.divergence_policy,
            "interception_surfaces": list(self.interception_surfaces),
            "dispatcher_installed": self.dispatcher_installed,
            "model_calls_recorded": self.model_calls_recorded,
            "model_calls_live": self.model_calls_live,
            "model_calls_unconsumed": self.model_calls_unconsumed,
            "model_errors_replayed": self.model_errors_replayed,
            "tool_calls_recorded": self.tool_calls_recorded,
            "tool_calls_not_interceptable": self.tool_calls_not_interceptable,
            "tool_calls_unconsumed": self.tool_calls_unconsumed,
            "tool_calls_refused": self.tool_calls_refused,
            "tool_calls_by_surface": {k: dict(v) for k, v in self.tool_calls_by_surface.items()},
            "model_calls_covered": self.model_calls_covered,
            "tool_calls_covered": self.tool_calls_covered,
            "tool_result_echo": self.tool_result_echo(),
            "network_observed": self.network_observed,
            "network_connections_live": self.network_connections_live,
            "network_destinations": self.network_destinations[:MAX_LISTED_NETWORK_DESTINATIONS],
            "divergences": listed,
        }
        if self.network_connections_capped:
            out["network_connections_capped"] = True
        if self.tool_overrides:
            out["tool_overrides"] = [dict(o) for o in self.tool_overrides]
        if len(self.divergences) > len(listed):
            out["divergences_not_listed"] = len(self.divergences) - len(listed)
        return out


def surfaces_for(substitute_tools: bool) -> list[str]:
    surfaces = [f"{name} ({MODEL_CALL_MODES})" for name in MODEL_SURFACES.values()]
    if substitute_tools:
        surfaces.extend(TOOL_SURFACES)
    return surfaces


#: Divergence kinds counted as a refused/unmatched *tool* call.
_TOOL_DIVERGENCE_KINDS = frozenset({"tool_call_unmatched", "tool_result_not_servable"})

_SURFACE_COUNTERS = (
    "recorded", "available", "mocked", "live", "refused", "unmatched", "covered",
    "unconsumed",
)


def summarize(
    model_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    divergence_policy: str,
    substitute_tools: bool,
) -> ReplayContractReport:
    """Fold the dispatchers' event log into a :class:`ReplayContractReport`."""
    queues = model_queues(model_calls)
    tools = interceptable_tool_calls(tool_calls)
    by_surface: dict[str, dict[str, int]] = {
        surface: dict.fromkeys(_SURFACE_COUNTERS, 0) for surface in TOOL_SURFACES
    }
    for rec in tools:
        counters = by_surface[str(tool_surface(rec))]
        counters["recorded"] += 1
        if not_servable_reason(rec) is None:
            counters["available"] += 1
    consumed_unserved: dict[str, int] = dict.fromkeys(TOOL_SURFACES, 0)
    report = ReplayContractReport(
        divergence_policy=divergence_policy,
        substitute_tools=substitute_tools,
        interception_surfaces=surfaces_for(substitute_tools),
        model_calls_recorded=len(model_calls),
        model_calls_available=sum(len(q) for q in queues.values()),
        tool_calls_recorded=len(tool_calls),
        tool_calls_available=sum(c["available"] for c in by_surface.values()),
        tool_calls_not_interceptable=len(tool_calls) - len(tools),
    )

    def _surface_of(ev: dict[str, Any]) -> dict[str, int]:
        surface = ev.get("surface") or TOOL_SURFACE_MCP
        return by_surface.setdefault(str(surface), dict.fromkeys(_SURFACE_COUNTERS, 0))

    served: dict[str, int] = dict.fromkeys(queues, 0)
    covered: dict[str, int] = dict.fromkeys(queues, 0)
    serving_pids: set[Any] = set()
    for ev in events:
        kind = ev.get("event")
        if kind == "installed":
            report.dispatcher_installed = True
        elif kind == "install_failed":
            report.divergences.append({
                "kind": "dispatcher_install_failed",
                "message": (
                    "the replay dispatcher could not be installed in the replayed "
                    f"process: {ev.get('error', 'unknown error')}"
                ),
            })
        elif kind == "model_served":
            queue = str(ev.get("queue") or ev.get("provider"))
            served[queue] = served.get(queue, 0) + 1
            report.model_calls_mocked += 1
            if ev.get("recorded_error") and ev.get("faithful", True):
                report.model_errors_replayed += 1
            serving_pids.add(ev.get("pid"))
        elif kind == "model_live":
            report.model_calls_live += 1
            serving_pids.add(ev.get("pid"))
        elif kind == "model_covered":
            queue = str(ev.get("queue"))
            covered[queue] = covered.get(queue, 0) + 1
            report.model_calls_covered += 1
        elif kind == "tool_covered":
            report.tool_calls_covered += 1
            _surface_of(ev)["covered"] += 1
        elif kind == "tool_result_echo":
            outcome = ev.get("outcome")
            if outcome == "matched":
                report.echo_matched += 1
            elif outcome == "mismatched":
                report.echo_mismatches.append({
                    "kind": TOOL_RESULT_ECHO_MISMATCH,
                    "message": (
                        f"tool result {ev.get('tool_call_id')} at {ev.get('surface')} call "
                        f"#{int(ev.get('call_index') or 0) + 1}: "
                        f"{ev.get('reason') or ECHO_DIFFERS} -- the workload's own code "
                        "produced a different tool result than at capture, so the "
                        "answers served after it respond to a different question "
                        "(report-only, ADR-0306 D10)"
                    ),
                    **{
                        k: ev[k]
                        for k in (
                            "tool_call_id", "surface", "call_index", "model_call_id", "reason",
                        )
                        if k in ev
                    },
                })
            else:
                why = str(ev.get("reason") or "not checked")
                report.echo_not_checked[why] = report.echo_not_checked.get(why, 0) + 1
        elif kind == "network_observer_installed":
            report.network_observed = True
        elif kind == "network_live":
            report.network_connections_live += 1
            destination = f"{ev.get('host')}:{ev.get('port')}"
            if destination not in report.network_destinations:
                report.network_destinations.append(destination)
        elif kind == "network_live_capped":
            report.network_connections_capped = True
        elif kind == "tool_mocked":
            report.tool_calls_mocked += 1
            _surface_of(ev)["mocked"] += 1
        elif kind == "tool_live":
            report.tool_calls_live += 1
            _surface_of(ev)["live"] += 1
            if ev.get("consumed"):
                # ADR-0306 D8: an honoured `allow: true` re-executes a call
                # whose record it consumed -- requested, so not left over.
                surface = str(ev.get("surface") or TOOL_SURFACE_MCP)
                consumed_unserved[surface] = consumed_unserved.get(surface, 0) + 1
        elif kind == "tool_refused":
            report.tool_calls_refused += 1
            _surface_of(ev)["refused"] += 1
        elif kind == "divergence":
            entry = {k: v for k, v in ev.items() if k != "event"}
            report.divergences.append(entry)
            if entry.get("kind") in {
                "model_queue_exhausted",
                "provider_mismatch",
                "order_mismatch",
                "unsupported_surface",
                "malformed_recorded_response",
                "recorded_error_unreconstructable",
            }:
                if entry.get("kind") != "unsupported_surface" or divergence_policy == "fail":
                    report.model_calls_unmatched += 1
            elif entry.get("kind") in _TOOL_DIVERGENCE_KINDS:
                report.tool_calls_unmatched += 1
                counters = _surface_of(entry)
                counters["unmatched"] += 1
                if entry.get("consumed"):
                    surface = str(entry.get("surface") or TOOL_SURFACE_MCP)
                    consumed_unserved[surface] = consumed_unserved.get(surface, 0) + 1

    leftovers = {
        p: len(q) - served[p] - covered.get(p, 0)
        for p, q in queues.items()
        if len(q) > served[p] + covered.get(p, 0)
    }
    report.model_calls_unconsumed = sum(leftovers.values())
    if report.model_calls_unconsumed:
        detail = ", ".join(
            f"{p}: {n} of {len(queues[p])} unconsumed" for p, n in sorted(leftovers.items())
        )
        message = f"recorded model responses were never requested by the replay ({detail})"
        if not report.dispatcher_installed:
            message += (
                "; the replay dispatcher was never installed in the replayed process "
                "(is the capsule command a Python interpreter?), so any model call it "
                "made went to the network"
            )
        report.divergences.append(
            {"kind": "model_calls_unconsumed", "message": message, "unconsumed": leftovers}
        )
    if substitute_tools:
        for surface, counters in by_surface.items():
            counters["unconsumed"] = max(
                0,
                counters["recorded"] - counters["mocked"] - counters["covered"]
                - consumed_unserved.get(surface, 0),
            )
        report.tool_calls_unconsumed = sum(c["unconsumed"] for c in by_surface.values())
        report.tool_calls_by_surface = by_surface
        if report.tool_calls_unconsumed:
            leftover = [
                (surface, c["unconsumed"], c["recorded"])
                for surface, c in by_surface.items() if c["unconsumed"]
            ]
            if len(leftover) == 1:
                surface, n, recorded = leftover[0]
                message = (
                    f"{n} of {recorded} recorded {surface} results were never "
                    "requested by the replay"
                )
            else:
                detail = "; ".join(f"{s}: {n} of {r}" for s, n, r in leftover)
                message = (
                    f"{report.tool_calls_unconsumed} of {len(tools)} recorded tool "
                    f"results were never requested by the replay ({detail})"
                )
            report.divergences.append({
                "kind": "tool_calls_unconsumed",
                "message": message,
                "unconsumed": {s: n for s, n, _ in leftover},
            })
    if len({p for p in serving_pids if p is not None}) > 1:
        report.divergences.append({
            "kind": "multiple_interpreters",
            "message": (
                f"{len(serving_pids)} Python processes each consumed the model queue "
                "from its start; recorded responses may have been served twice"
            ),
        })
    return report
