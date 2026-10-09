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

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from novafabric.capture.hooks._sdk_streams import (
    ANTHROPIC_MESSAGES_SURFACE,
    API_SURFACE_EXT,
    OPENAI_CHAT_SURFACE,
    OPENAI_RESPONSES_SURFACE,
)
from novafabric.capture.record_roles import is_transport_record

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

#: The one tool surface mocked replay intercepts.
TOOL_SURFACE_MCP = "mcp.ClientSession.call_tool"

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
    servable = [r for r in model_calls if is_replayable_model_call(r)]
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


def model_queues(model_calls: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per-surface queues of servable records, in recorded order."""
    queues: dict[str, list[dict[str, Any]]] = {q: [] for q in MODEL_SURFACES}
    for record in model_calls:
        if is_replayable_model_call(record):
            queues[str(model_queue_key(record))].append(record)
    return queues


def recorded_provider_order(model_calls: list[dict[str, Any]]) -> list[str]:
    """The queue of each servable record, in recorded order."""
    return [
        str(model_queue_key(r)) for r in model_calls if is_replayable_model_call(r)
    ]


# ── tool calls ───────────────────────────────────────────────────────────────


def normalized_arg_hash(arguments: Any) -> str:
    """Order-insensitive digest of tool arguments.

    ``None`` is the same as ``{}`` (the MCP hook records ``arguments or {}``);
    any other value is hashed as itself -- a non-dict is never collapsed into
    ``{}``, which would make unrelated calls collide.
    """
    if arguments is None:
        arguments = {}
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _mcp_method(record: dict[str, Any]) -> str:
    mcp = record.get("mcp")
    if isinstance(mcp, dict) and isinstance(mcp.get("method"), str):
        return str(mcp["method"])
    return "tools/call"


def _is_proxy_record(record: dict[str, Any]) -> bool:
    ext = record.get("extensions")
    return isinstance(ext, dict) and ext.get("io.novafabric.capture_method") == "proxy"


def is_interceptable_tool_call(record: dict[str, Any]) -> bool:
    """Recorded on the intercepted surface: an MCP ``tools/call`` with a name."""
    name = record.get("tool_name")
    return (
        record.get("transport") == "mcp"
        and _mcp_method(record) == "tools/call"
        and isinstance(name, str)
        and bool(name)
    )


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


@dataclass
class ToolMatch:
    """Outcome of matching one replayed tool call."""

    record: dict[str, Any] | None
    how: Literal["id", "signature"] | None = None
    reason: str | None = None


class ToolCallMatcher:
    """One-to-one matching of replayed tool calls to recorded ones.

    Order of preference:

    1. exact ``tool_call_id`` (when the surface carries one) with the same name;
    2. tool name + normalized argument hash, earliest unconsumed record first --
       so repeated identical calls consume distinct records in recorded order;
    3. otherwise **unmatched**, with a reason. A record is never served twice:
       this is the dict-keyed-by-signature bug class ``nova diff`` once had.
    """

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = list(records)
        self._hashes = [normalized_arg_hash(r.get("arguments")) for r in self._records]
        self._consumed = [False] * len(self._records)

    def __len__(self) -> int:
        return len(self._records)

    @property
    def consumed_count(self) -> int:
        return sum(self._consumed)

    def unconsumed(self) -> list[dict[str, Any]]:
        return [r for r, used in zip(self._records, self._consumed) if not used]

    def _take(self, idx: int, how: Literal["id", "signature"]) -> ToolMatch:
        self._consumed[idx] = True
        return ToolMatch(record=self._records[idx], how=how)

    def match(
        self, tool_call_id: str | None, tool_name: str, arguments: Any
    ) -> ToolMatch:
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
        digest = normalized_arg_hash(arguments)
        for idx, rec in enumerate(self._records):
            if (
                not self._consumed[idx]
                and rec.get("tool_name") == tool_name
                and self._hashes[idx] == digest
            ):
                return self._take(idx, "signature")
        same_name = [i for i, r in enumerate(self._records) if r.get("tool_name") == tool_name]
        if any(self._hashes[i] == digest for i in same_name):
            reason = "every recorded call with these arguments was already consumed"
        elif same_name:
            reason = "the tool was recorded, but not with these arguments"
        else:
            reason = "no recorded call to this tool"
        return ToolMatch(record=None, reason=reason)


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
    tool_calls_recorded: int = 0
    tool_calls_available: int = 0
    tool_calls_not_interceptable: int = 0
    tool_calls_mocked: int = 0
    tool_calls_live: int = 0
    tool_calls_unmatched: int = 0
    tool_calls_unconsumed: int = 0
    network_observed: bool = False
    network_connections_live: int = 0
    network_connections_capped: bool = False
    network_destinations: list[str] = field(default_factory=list)
    divergences: list[dict[str, Any]] = field(default_factory=list)

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

    def as_dict(self) -> dict[str, Any]:
        listed = self.divergences[:MAX_LISTED_DIVERGENCES]
        out: dict[str, Any] = {
            "divergence_policy": self.divergence_policy,
            "interception_surfaces": list(self.interception_surfaces),
            "dispatcher_installed": self.dispatcher_installed,
            "model_calls_recorded": self.model_calls_recorded,
            "model_calls_live": self.model_calls_live,
            "model_calls_unconsumed": self.model_calls_unconsumed,
            "tool_calls_recorded": self.tool_calls_recorded,
            "tool_calls_not_interceptable": self.tool_calls_not_interceptable,
            "tool_calls_unconsumed": self.tool_calls_unconsumed,
            "network_observed": self.network_observed,
            "network_connections_live": self.network_connections_live,
            "network_destinations": self.network_destinations[:MAX_LISTED_NETWORK_DESTINATIONS],
            "divergences": listed,
        }
        if self.network_connections_capped:
            out["network_connections_capped"] = True
        if len(self.divergences) > len(listed):
            out["divergences_not_listed"] = len(self.divergences) - len(listed)
        return out


def surfaces_for(substitute_tools: bool) -> list[str]:
    surfaces = [f"{name} ({MODEL_CALL_MODES})" for name in MODEL_SURFACES.values()]
    if substitute_tools:
        surfaces.append(TOOL_SURFACE_MCP)
    return surfaces


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
    report = ReplayContractReport(
        divergence_policy=divergence_policy,
        substitute_tools=substitute_tools,
        interception_surfaces=surfaces_for(substitute_tools),
        model_calls_recorded=len(model_calls),
        model_calls_available=sum(len(q) for q in queues.values()),
        tool_calls_recorded=len(tool_calls),
        tool_calls_available=len(tools),
        tool_calls_not_interceptable=len(tool_calls) - len(tools),
    )
    served: dict[str, int] = dict.fromkeys(queues, 0)
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
            serving_pids.add(ev.get("pid"))
        elif kind == "model_live":
            report.model_calls_live += 1
            serving_pids.add(ev.get("pid"))
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
        elif kind == "tool_live":
            report.tool_calls_live += 1
        elif kind == "divergence":
            entry = {k: v for k, v in ev.items() if k != "event"}
            report.divergences.append(entry)
            if entry.get("kind") in {
                "model_queue_exhausted",
                "provider_mismatch",
                "order_mismatch",
                "unsupported_surface",
                "malformed_recorded_response",
            }:
                if entry.get("kind") != "unsupported_surface" or divergence_policy == "fail":
                    report.model_calls_unmatched += 1
            elif entry.get("kind") == "tool_call_unmatched":
                report.tool_calls_unmatched += 1

    leftovers = {p: len(q) - served[p] for p, q in queues.items() if len(q) > served[p]}
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
        report.tool_calls_unconsumed = max(0, len(tools) - report.tool_calls_mocked)
        if report.tool_calls_unconsumed:
            report.divergences.append({
                "kind": "tool_calls_unconsumed",
                "message": (
                    f"{report.tool_calls_unconsumed} of {len(tools)} recorded "
                    f"{TOOL_SURFACE_MCP} results were never requested by the replay"
                ),
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
