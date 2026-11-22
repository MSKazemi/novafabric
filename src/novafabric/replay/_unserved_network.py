"""The strict-mode ``unserved_network`` rule (ADR-0319 amendment, replay strategy W0).

A mocked replay serves what it serves *in-process*: the SDK hooks (OpenAI,
Anthropic), the ``httpx`` transport (Ollama), the ``urllib3`` pool (AWS Bedrock)
and the MCP / ``record.tool`` / framework tool seams answer from the capsule
without opening a socket. So an IPv4/IPv6 connect attempt to a destination other
than this machine, made by an observed process of the replayed tree, is one no
served surface answered: a model provider replay does not know (Google Gemini /
Vertex before they were in the URL registry, a gRPC transport, a new provider), a
raw-HTTP tool, a package-index version check in an MCP server child. Before this
rule such an attempt was only counted (``network_connections_live``), and a strict
replay could still end ``success``: a silent fail-open.

The rule:

* every observed non-loopback attempt is counted (``network_connections_unserved``,
  ``network_destinations_unserved``), whatever the policy;
* under the strict policy (``fail``, the default) any such attempt is the
  divergence :data:`UNSERVED_NETWORK`, so the replay fails;
* under ``--permissive`` (``warn``) it is counted and reported, never a divergence;
* loopback (127.0.0.0/8, ``::1``, ``localhost``; ``_child_observer.is_loopback_host``)
  and AF_UNIX sockets stay allowed: counted in ``network_connections_live`` as
  before, never unserved. A loopback service can still answer live -- that is what
  the fidelity verdict's ``live_calls`` check (ADR-0321) keeps reporting.

What the rule cannot see is what the observers cannot see: sockets opened by C code
that bypasses ``socket.socket.connect``, and processes that did not report in
(``child_processes_unobserved``, ``network_observation_coverage: partial``) -- the
divergence message then says its count is a lower bound; ``--hermetic`` blocks
those instead. Pure functions; no import-time IO.
"""

from __future__ import annotations

from typing import Any

from novafabric.replay._child_observer import (
    UNSERVED_NETWORK_AFTER_CAP_EVENT,
    is_loopback_host,
)

__all__ = [
    "MAX_LISTED_UNSERVED_DESTINATIONS",
    "POLICY_DIVERGENCE",
    "POLICY_REPORTED",
    "UNSERVED_NETWORK",
    "UNSERVED_NETWORK_AFTER_CAP_EVENT",
    "is_loopback_host",
    "policy_for",
    "unserved_network_divergence",
]

#: The divergence kind.
UNSERVED_NETWORK = "unserved_network"

#: ``unserved_network_policy`` of a strict replay: an unserved attempt diverges.
POLICY_DIVERGENCE = "divergence"
#: ``unserved_network_policy`` under ``--permissive``: counted and reported only.
POLICY_REPORTED = "reported"

#: Most distinct unserved ``host:port`` destinations a result lists.
MAX_LISTED_UNSERVED_DESTINATIONS = 20


def policy_for(divergence_policy: str) -> str:
    """The ``unserved_network_policy`` of a replay run under *divergence_policy*."""
    return POLICY_REPORTED if divergence_policy == "warn" else POLICY_DIVERGENCE


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def unserved_network_divergence(
    *,
    divergence_policy: str,
    connections: int,
    connections_children: int,
    destinations: list[str],
    lower_bound: bool,
) -> dict[str, Any] | None:
    """The ``unserved_network`` divergence of a replay, or ``None``.

    ``None`` under ``--permissive`` and when no unserved attempt was observed.
    *lower_bound*: some attempts may not have been seen (the event cap, an
    unobserved child process), so *connections* is a lower bound.
    """
    if policy_for(divergence_policy) != POLICY_DIVERGENCE or connections <= 0:
        return None
    listed = destinations[:MAX_LISTED_UNSERVED_DESTINATIONS]
    where = ", ".join(listed) + (
        f" (+{len(destinations) - len(listed)} more)" if len(destinations) > len(listed) else ""
    )
    who = (
        f", {connections_children} of them by observed child processes"
        if connections_children else ""
    )
    message = (
        f"{_plural(connections, 'non-loopback connection attempt')} no served surface "
        f"answered ({where}){who}: the replay reached, or tried to reach, a live service "
        "the capsule does not answer (an unregistered model provider, gRPC, a raw-HTTP "
        "tool). A strict replay fails on unserved network; --permissive reports it instead"
    )
    if lower_bound:
        message += (
            "; a lower bound -- the observers did not see every connection "
            "(event cap or network_observation_coverage: partial)"
        )
    return {
        "kind": UNSERVED_NETWORK,
        "message": message,
        "connections": connections,
        "connections_children": connections_children,
        "destinations": listed,
    }
