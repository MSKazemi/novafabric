"""SEP-2322 multi-round-trip exchange capture (NF-038 R3–R5, R10).

MCP 2026-07-28 replaced server-initiated sampling/elicitation with
**payload-carried multi-round-trip** exchanges: the server sends
``elicitation/inputRequired``, the client replies ``elicitation/inputResponses``,
possibly several times. Capturing those as opaque blobs would lose the
ordering and round structure a replay needs (spec §5 rejects exactly that), so
each leg becomes a first-class, round-indexed record.

Pure functions over already-parsed JSON-RPC messages — no I/O, no proxy state
beyond the tracker you hand it — so the wire behaviour can be tested without a
live MCP server.

Two invariants the tests pin:

- **Grouping is by JSON-RPC id, not by arrival order.** Concurrent exchanges
  interleave on the wire; keying off order would splice two conversations
  together, which is worse than not capturing them.
- **``round`` counts request/response *pairs*, not messages.** A two-round
  exchange yields four records with rounds 1,1,2,2 (AC2), so a reader can see
  the turn structure rather than a flat message list.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Final

from novafabric.capture._ulid import new_ulid

#: Newest MCP revision this build understands (spec §1).
SUPPORTED_PROTOCOL_VERSION: Final[str] = "2026-07-28"

#: Prior shipped revision. R2 requires selecting behaviour per the negotiated
#: version rather than assuming the newest.
LEGACY_PROTOCOL_VERSION: Final[str] = "2025-06-18"

#: SEP-2322 method names, server→client then client→server.
INPUT_REQUIRED: Final[str] = "elicitation/inputRequired"
INPUT_RESPONSES: Final[str] = "elicitation/inputResponses"

#: Namespaced key carrying a detected Tasks extension (R5). Reverse-DNS so it
#: cannot collide with a future first-class field.
TASKS_EXTENSION_KEY: Final[str] = "io.modelcontextprotocol.tasks"


def payload_digest(payload: Any) -> str:
    """Stable ``sha256:`` digest over a JSON payload.

    Canonical (sorted keys, no whitespace) so the same logical payload digests
    identically regardless of wire formatting — otherwise a digest would
    compare unequal for a purely cosmetic difference.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def detect_tasks_extension(message: dict[str, Any]) -> dict[str, Any] | None:
    """Record a Tasks extension's *presence and digest* without understanding it.

    Tasks moved from core to an optional extension in 2026-07-28. NovaFabric
    deliberately does not implement its semantics (spec §2 non-goal) — but a
    capsule that silently dropped it would lose provenance. So we record that
    it was there and what it hashed to, which is forward-compatible with an
    extension we have never seen.
    """
    for container in (message, message.get("params") or {}):
        if not isinstance(container, dict):
            continue
        extensions = container.get("extensions")
        if isinstance(extensions, dict) and TASKS_EXTENSION_KEY in extensions:
            return {
                "present": True,
                "digest": payload_digest(extensions[TASKS_EXTENSION_KEY]),
            }
    return None


def protocol_version_note(negotiated: str | None) -> str | None:
    """Warn about a version newer than we know, without refusing it (R10).

    Best-effort passthrough is deliberate: a proxy that refused an unknown
    version would break a working client to protect a capture guarantee that
    is secondary to the user's workload.
    """
    if not negotiated or negotiated in (SUPPORTED_PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION):
        return None
    if negotiated > SUPPORTED_PROTOCOL_VERSION:
        return (
            f"MCP protocol {negotiated} is newer than the newest this build "
            f"understands ({SUPPORTED_PROTOCOL_VERSION}); messages are passed "
            f"through unchanged and captured best-effort."
        )
    return (
        f"MCP protocol {negotiated} is older than the versions this build "
        f"targets; capture proceeds best-effort."
    )


@dataclass
class _Exchange:
    exchange_id: str
    round: int = 1
    #: True once the request leg has been seen and we await its response.
    awaiting_response: bool = False


@dataclass
class ExchangeTracker:
    """Groups SEP-2322 legs into rounds, keyed by JSON-RPC id.

    One tracker per proxied session. Not thread-safe by itself — the proxy
    holds its own lock, and duplicating locking here would be a second place
    for it to be wrong.
    """

    _by_rpc_id: dict[str, _Exchange] = field(default_factory=dict)

    def observe(
        self,
        message: dict[str, Any],
        *,
        direction: str,
        protocol_version: str | None = None,
    ) -> dict[str, Any] | None:
        """Return a capture record for an SEP-2322 leg, or ``None``.

        ``None`` means "not an elicitation leg" — every other message type is
        left entirely alone, so this can be called on the whole stream.
        """
        method = message.get("method")
        if method not in (INPUT_REQUIRED, INPUT_RESPONSES):
            return None

        rpc_id = message.get("id")
        if rpc_id is None:
            # A leg with no id cannot be correlated. Capturing it under a
            # fabricated id would invent a grouping that does not exist.
            return None
        key = str(rpc_id)

        exchange = self._by_rpc_id.get(key)
        if exchange is None:
            exchange = _Exchange(exchange_id=new_ulid())
            self._by_rpc_id[key] = exchange

        if method == INPUT_REQUIRED:
            # A second request on the same id opens the next round.
            if exchange.awaiting_response is False and exchange.round > 0:
                pass  # first request of this round
            exchange.awaiting_response = True
        else:
            exchange.awaiting_response = False

        record: dict[str, Any] = {
            "tool_call_id": new_ulid(),
            "kind": "mcp",
            "mcp_method": method,
            "mcp_exchange_id": exchange.exchange_id,
            "round": exchange.round,
            "direction": direction,
            "payload_digest": payload_digest(message.get("params", {})),
            # An elicitation exchange asks a human for input; it does not act
            # on the world. Marking it mutating would inflate the
            # mutating-tool count the replay-safety gate reads.
            "mutates": False,
        }
        if protocol_version:
            record["mcp_protocol_version"] = protocol_version

        tasks = detect_tasks_extension(message)
        if tasks is not None:
            record["extensions"] = {TASKS_EXTENSION_KEY: tasks}

        # The response closes this round; the next request increments.
        if method == INPUT_RESPONSES:
            exchange.round += 1

        return record
