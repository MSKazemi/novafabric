"""NF-038 R9 — replay the conformance vectors and assert wire compatibility.

Each vector in ``vectors/`` records an MCP 2026-07-28 exchange plus the
capture shape it must produce. They exist so a **spec drift fails a PR**
rather than surfacing as silently-wrong evidence months later.

The vector's ``why`` field is printed on failure: a conformance failure
should tell you what breaks in the product, not just which assert tripped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.mcp.exchanges import TASKS_EXTENSION_KEY, ExchangeTracker

VECTOR_DIR = Path(__file__).parent / "vectors"


def _vectors() -> list[tuple[str, dict[str, Any]]]:
    return [
        (path.stem, json.loads(path.read_text()))
        for path in sorted(VECTOR_DIR.glob("*.json"))
    ]


def replay(vector: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a vector's messages through a fresh tracker."""
    tracker = ExchangeTracker()
    records: list[dict[str, Any]] = []
    for leg in vector["messages"]:
        record = tracker.observe(
            leg["message"],
            direction=leg["direction"],
            protocol_version=vector.get("protocol_version"),
        )
        if record is not None:
            records.append(record)
    return records


def test_vector_directory_is_not_empty() -> None:
    """A conformance suite that silently has no vectors proves nothing."""
    assert _vectors(), f"no conformance vectors found in {VECTOR_DIR}"


@pytest.mark.parametrize("name,vector", _vectors(), ids=lambda v: v if isinstance(v, str) else "")
def test_vector(name: str, vector: dict[str, Any]) -> None:
    why = vector.get("why", "(no rationale recorded)")
    records = replay(vector)
    expect = vector["expect"]

    assert len(records) == expect["record_count"], (
        f"{vector['name']}: expected {expect['record_count']} records, "
        f"got {len(records)}.\nWhy this matters: {why}"
    )

    if "rounds" in expect:
        assert [r["round"] for r in records] == expect["rounds"], (
            f"{vector['name']}: round structure drifted.\nWhy: {why}"
        )

    if "directions" in expect:
        assert [r["direction"] for r in records] == expect["directions"], (
            f"{vector['name']}: leg directions drifted.\nWhy: {why}"
        )

    if expect.get("shared_exchange_id"):
        ids = {r["mcp_exchange_id"] for r in records}
        assert len(ids) == 1, (
            f"{vector['name']}: legs split across {len(ids)} exchange ids.\nWhy: {why}"
        )

    if "distinct_exchange_ids" in expect:
        ids = {r["mcp_exchange_id"] for r in records}
        assert len(ids) == expect["distinct_exchange_ids"], (
            f"{vector['name']}: expected {expect['distinct_exchange_ids']} distinct "
            f"exchanges, got {len(ids)}.\nWhy: {why}"
        )

    if expect.get("tasks_extension_present"):
        assert any(
            TASKS_EXTENSION_KEY in (r.get("extensions") or {}) for r in records
        ), f"{vector['name']}: Tasks extension was dropped.\nWhy: {why}"

    for forbidden in expect.get("no_raw_values", []):
        blob = json.dumps(records)
        assert forbidden not in blob, (
            f"{vector['name']}: raw value {forbidden!r} leaked into the capture "
            f"record.\nWhy: {why}"
        )


def test_every_vector_records_why_it_exists() -> None:
    """A vector without a rationale is a assertion nobody can safely change.

    When one fails years from now, the person deciding whether the behaviour
    or the vector is wrong needs to know what it was protecting.
    """
    for name, vector in _vectors():
        assert vector.get("why"), f"vector {name} has no 'why' field"
        assert len(vector["why"]) > 40, f"vector {name} needs a substantive 'why'"
