"""NF-038 R3–R5, R10 — SEP-2322 multi-round-trip exchange capture."""

from __future__ import annotations

from novafabric.mcp.exchanges import (
    INPUT_REQUIRED,
    INPUT_RESPONSES,
    LEGACY_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSION,
    TASKS_EXTENSION_KEY,
    ExchangeTracker,
    detect_tasks_extension,
    payload_digest,
    protocol_version_note,
)


def _req(rpc_id, **params):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": INPUT_REQUIRED, "params": params}


def _resp(rpc_id, **params):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": INPUT_RESPONSES, "params": params}


# ---------------------------------------------------------------------------
# AC2 — a two-round exchange yields rounds 1,1,2,2 with one exchange id
# ---------------------------------------------------------------------------


def test_two_round_exchange_shares_one_id_with_rounds_1122() -> None:
    t = ExchangeTracker()
    records = [
        t.observe(_req(7, prompt="name?"), direction="server_to_client"),
        t.observe(_resp(7, value="ada"), direction="client_to_server"),
        t.observe(_req(7, prompt="confirm?"), direction="server_to_client"),
        t.observe(_resp(7, value="yes"), direction="client_to_server"),
    ]
    assert all(r is not None for r in records)
    ids = {r["mcp_exchange_id"] for r in records}
    assert len(ids) == 1, "all legs of one exchange must share an id"
    assert [r["round"] for r in records] == [1, 1, 2, 2]
    assert [r["direction"] for r in records] == [
        "server_to_client", "client_to_server", "server_to_client", "client_to_server",
    ]


def test_concurrent_exchanges_are_grouped_by_rpc_id_not_arrival_order() -> None:
    """Interleaved exchanges must not be spliced together.

    Keying off arrival order would merge two conversations, which is worse
    than not capturing them at all.
    """
    t = ExchangeTracker()
    a1 = t.observe(_req("A", p=1), direction="server_to_client")
    b1 = t.observe(_req("B", p=2), direction="server_to_client")
    a2 = t.observe(_resp("A", v=1), direction="client_to_server")
    b2 = t.observe(_resp("B", v=2), direction="client_to_server")

    assert a1["mcp_exchange_id"] == a2["mcp_exchange_id"]
    assert b1["mcp_exchange_id"] == b2["mcp_exchange_id"]
    assert a1["mcp_exchange_id"] != b1["mcp_exchange_id"]


def test_non_elicitation_messages_are_left_alone() -> None:
    t = ExchangeTracker()
    assert t.observe({"method": "tools/call", "id": 1}, direction="client_to_server") is None
    assert t.observe({"method": "initialize", "id": 2}, direction="client_to_server") is None
    assert t.observe({"result": {}, "id": 3}, direction="server_to_client") is None


def test_leg_without_an_id_is_not_captured() -> None:
    """Correlating under a fabricated id would invent a grouping."""
    t = ExchangeTracker()
    assert t.observe({"method": INPUT_REQUIRED, "params": {}}, direction="server_to_client") is None


def test_elicitation_is_never_marked_mutating() -> None:
    """It asks a human for input; it does not act on the world.

    Marking it mutating would inflate the mutating-tool count the
    replay-safety gate reads.
    """
    t = ExchangeTracker()
    assert t.observe(_req(1), direction="server_to_client")["mutates"] is False


def test_protocol_version_is_recorded_when_known() -> None:
    t = ExchangeTracker()
    rec = t.observe(_req(1), direction="server_to_client", protocol_version="2026-07-28")
    assert rec["mcp_protocol_version"] == "2026-07-28"


# ---------------------------------------------------------------------------
# R5 — Tasks extension detected and captured, never executed
# ---------------------------------------------------------------------------


def test_tasks_extension_is_recorded_by_presence_and_digest() -> None:
    t = ExchangeTracker()
    rec = t.observe(
        {
            "id": 1,
            "method": INPUT_REQUIRED,
            "params": {"extensions": {TASKS_EXTENSION_KEY: {"taskId": "t-1"}}},
        },
        direction="server_to_client",
    )
    ext = rec["extensions"][TASKS_EXTENSION_KEY]
    assert ext["present"] is True
    assert ext["digest"].startswith("sha256:")


def test_tasks_extension_detected_at_message_level_too() -> None:
    assert detect_tasks_extension({"extensions": {TASKS_EXTENSION_KEY: {}}}) is not None


def test_absent_tasks_extension_adds_no_key() -> None:
    t = ExchangeTracker()
    assert "extensions" not in t.observe(_req(1), direction="server_to_client")


# ---------------------------------------------------------------------------
# Digests + R10 version handling
# ---------------------------------------------------------------------------


def test_digest_is_canonical_across_key_order() -> None:
    """Cosmetic wire differences must not change the digest."""
    assert payload_digest({"a": 1, "b": 2}) == payload_digest({"b": 2, "a": 1})


def test_known_versions_produce_no_warning() -> None:
    assert protocol_version_note(SUPPORTED_PROTOCOL_VERSION) is None
    assert protocol_version_note(LEGACY_PROTOCOL_VERSION) is None
    assert protocol_version_note(None) is None


def test_newer_version_warns_but_implies_passthrough() -> None:
    """R10: warn, but never refuse — the user's workload comes first."""
    note = protocol_version_note("2027-01-01")
    assert note is not None
    assert "newer" in note
    assert "passed through" in note


def test_older_version_is_noted_as_best_effort() -> None:
    note = protocol_version_note("2024-01-01")
    assert note is not None and "best-effort" in note


# ---------------------------------------------------------------------------
# R6 — elicited inputs must never reach the capsule as raw values
#
# Elicitation carries user-typed content, so it is a prime channel for a
# secret to enter evidence. This implementation satisfies R6 by construction:
# it records a DIGEST and never the payload. That is stronger than scanning
# would be — a scanner can miss a novel secret shape, an absent value cannot
# leak one. These tests prove the property rather than trusting the design.
# ---------------------------------------------------------------------------

_SECRET = "sk-ant-api03-averyrealsecretvalue0123456789"


def test_elicited_value_never_appears_in_the_record() -> None:
    t = ExchangeTracker()
    rec = t.observe(
        _resp(1, value=_SECRET, note=f"token={_SECRET}"),
        direction="client_to_server",
    )
    serialized = repr(rec)
    assert _SECRET not in serialized
    assert "token=" not in serialized


def test_record_carries_a_digest_not_a_payload() -> None:
    t = ExchangeTracker()
    rec = t.observe(_resp(1, value="anything"), direction="client_to_server")
    assert rec["payload_digest"].startswith("sha256:")
    # No key in the record holds the params themselves.
    assert "params" not in rec
    assert "value" not in rec


def test_tasks_extension_payload_is_digested_not_copied() -> None:
    """A Tasks payload could equally carry a secret."""
    t = ExchangeTracker()
    rec = t.observe(
        {
            "id": 1,
            "method": INPUT_REQUIRED,
            "params": {"extensions": {TASKS_EXTENSION_KEY: {"apiKey": _SECRET}}},
        },
        direction="server_to_client",
    )
    assert _SECRET not in repr(rec)
    assert rec["extensions"][TASKS_EXTENSION_KEY]["digest"].startswith("sha256:")


def test_digest_is_one_way_and_stable() -> None:
    """The digest must identify the payload without revealing it."""
    a = payload_digest({"value": _SECRET})
    b = payload_digest({"value": _SECRET})
    assert a == b
    assert _SECRET not in a
