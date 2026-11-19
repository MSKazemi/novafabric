"""Which recorded model calls mocked replay can serve (ADR-0319).

Capture records more model calls than mocked replay serves: the wire hook
classifies an HTTP call to any registry host as a model call (Ollama,
Hugging Face, other providers' raw HTTP) and writes it with ``gen_ai.system``
set but no response. Before ADR-0319 mocked replay ignored such a record --
neither served, counted nor refused -- and reported success while the call ran
live. Every recorded model call now falls in exactly one class:

``servable``
    on a surface mocked replay serves, with something to serve (a recorded
    response, or a recorded SDK error that holds a queue position);
``not_servable``
    a logical model call replay cannot serve: an unserved surface, or a served
    surface whose response was never recorded;
``transport``
    an ADR-0305 transport record (an HTTP attempt beneath an SDK call,
    including an orphaned one a count promotes): never a call to serve, so it
    is ignored here, exactly as the dispatcher ignores it.

The served surfaces are a table (:data:`SERVED_SURFACES`): a surface becomes
servable by adding a row that says which records it serves -- nothing else in
this module changes. Pure functions only; no import-time IO.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from novafabric.capture.record_roles import ROLE_TRANSPORT, classify_model_calls
from novafabric.replay._contract import is_replayable_model_call, is_replayable_model_error

Servability = Literal["servable", "not_servable", "transport"]
SERVABLE: Servability = "servable"
NOT_SERVABLE: Servability = "not_servable"
TRANSPORT: Servability = "transport"

#: ``gen_ai.system`` reported for a record that carries none (or is not a dict).
UNKNOWN_SYSTEM = "unknown"

#: Most distinct endpoint hosts listed per ``gen_ai.system``.
MAX_LISTED_HOSTS = 10


@dataclass(frozen=True)
class ServedSurface:
    """One model surface mocked replay serves from the capsule.

    ``serves(record)`` is True when *record* -- a logical model call -- holds a
    position this surface serves (a response, or a recorded error it raises).
    """

    name: str
    serves: Callable[[dict[str, Any]], bool]


def _sdk_queue_position(record: dict[str, Any]) -> bool:
    """The OpenAI / Anthropic SDK queues of ``_contract.model_queues``."""
    return is_replayable_model_call(record) or is_replayable_model_error(record)


#: The registry: every model surface mocked replay serves. A record no row
#: serves is ``not_servable``.
SERVED_SURFACES: tuple[ServedSurface, ...] = (
    ServedSurface(
        name=(
            "OpenAI Chat Completions / Responses API, Anthropic Messages "
            "(SDK hooks; ADR-0300, ADR-0304)"
        ),
        serves=_sdk_queue_position,
    ),
)


def is_servable(record: Any) -> bool:
    """Some row of :data:`SERVED_SURFACES` serves this (logical) record."""
    return isinstance(record, dict) and any(s.serves(record) for s in SERVED_SURFACES)


def classify(records: Sequence[Any]) -> list[Servability]:
    """The servability of every record of a ``model-calls.jsonl``, in order.

    *records* is the whole stream (transport records included): the ADR-0305
    role of a record depends on the records around it.
    """
    out: list[Servability] = []
    for record, role in zip(records, classify_model_calls(list(records))):
        if role.role == ROLE_TRANSPORT:
            out.append(TRANSPORT)
        else:
            out.append(SERVABLE if is_servable(record) else NOT_SERVABLE)
    return out


def not_servable_records(records: Sequence[Any]) -> list[Any]:
    """The recorded model calls mocked replay cannot serve, in recorded order."""
    return [r for r, c in zip(records, classify(records)) if c == NOT_SERVABLE]


def system_of(record: Any) -> str:
    """The record's ``gen_ai.system``, or ``"unknown"``."""
    system = record.get("gen_ai.system") if isinstance(record, Mapping) else None
    return system if isinstance(system, str) and system else UNKNOWN_SYSTEM


def endpoint_host(record: Any) -> str | None:
    """``host[:port]`` of the record's ``endpoint`` -- never its userinfo,
    path or query, which may carry credentials. ``None`` when not recorded."""
    endpoint = record.get("endpoint") if isinstance(record, Mapping) else None
    if not isinstance(endpoint, str) or not endpoint:
        return None
    try:
        parts = urlsplit(endpoint)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


def not_servable_by_system(records: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """``{gen_ai.system: {"count": n, "hosts": [...]}}`` of the unservable calls.

    Systems in first-recorded order; up to :data:`MAX_LISTED_HOSTS` distinct
    endpoint hosts per system, in first-recorded order.
    """
    out: dict[str, dict[str, Any]] = {}
    for record in not_servable_records(records):
        entry = out.setdefault(system_of(record), {"count": 0, "hosts": []})
        entry["count"] += 1
        host = endpoint_host(record)
        if host is not None and host not in entry["hosts"]:
            if len(entry["hosts"]) < MAX_LISTED_HOSTS:
                entry["hosts"].append(host)
    return out


def describe_by_system(by_system: Mapping[str, Mapping[str, Any]]) -> str:
    """``ollama: 1 (127.0.0.1:11434); huggingface: 2 (host not recorded)``."""
    parts = []
    for system, entry in by_system.items():
        hosts = ", ".join(entry.get("hosts") or []) or "host not recorded"
        parts.append(f"{system}: {entry.get('count', 0)} ({hosts})")
    return "; ".join(parts)
