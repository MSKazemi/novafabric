"""Tests for CapsuleEventConsumer.

Tests local-dir source (plain directory and zip capsules).
NATS source tests are skipped unless NOVA_INTEGRATION=1.
"""
from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine in an isolated event loop.  Does not disturb the global event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _collect(async_iter) -> list[dict]:
    events = []
    async for event in async_iter:
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Local-dir source — plain directory capsules
# ---------------------------------------------------------------------------


def test_consumer_iter_from_empty_directory(tmp_path: Path) -> None:
    """iter_from_directory on an empty dir yields no events."""
    from novafabric.kg.consumer import CapsuleEventConsumer

    consumer = CapsuleEventConsumer()
    events = _run(_collect(consumer.iter_from_directory(tmp_path)))
    assert events == []


def test_consumer_iter_from_directory_model_calls_jsonl(tmp_path: Path) -> None:
    """iter_from_directory yields events from model-calls.jsonl in subdirectory."""
    capsule_dir = tmp_path / "capsule-001"
    capsule_dir.mkdir()
    sample_events = [
        {"event_type": "ModelCallCompleted", "agent_id": "agent-1", "model_id": "gpt-4o", "capsule_id": "cap-1"},
        {"event_type": "ToolCallCompleted", "agent_id": "agent-1", "tool_name": "read_file", "capsule_id": "cap-1"},
    ]
    (capsule_dir / "model-calls.jsonl").write_text(
        "\n".join(json.dumps(e) for e in sample_events)
    )

    from novafabric.kg.consumer import CapsuleEventConsumer

    consumer = CapsuleEventConsumer()
    events = _run(_collect(consumer.iter_from_directory(tmp_path)))
    assert len(events) == 2
    assert events[0]["event_type"] == "ModelCallCompleted"
    assert events[1]["event_type"] == "ToolCallCompleted"


def test_consumer_iter_from_directory_events_jsonl_fallback(tmp_path: Path) -> None:
    """Falls back to events.jsonl when model-calls.jsonl is absent."""
    capsule_dir = tmp_path / "capsule-002"
    capsule_dir.mkdir()
    sample = [{"event_type": "EndpointRouted", "agent_id": "a", "url": "https://api.openai.com/v1/", "capsule_id": "c"}]
    (capsule_dir / "events.jsonl").write_text(json.dumps(sample[0]))

    from novafabric.kg.consumer import CapsuleEventConsumer

    consumer = CapsuleEventConsumer()
    events = _run(_collect(consumer.iter_from_directory(tmp_path)))
    assert len(events) == 1
    assert events[0]["event_type"] == "EndpointRouted"


def test_consumer_skips_bad_json_lines(tmp_path: Path) -> None:
    """Malformed JSON lines are skipped without aborting iteration."""
    capsule_dir = tmp_path / "capsule-003"
    capsule_dir.mkdir()
    content = (
        '{"event_type": "ModelCallCompleted", "agent_id": "a", "model_id": "gpt-4o", "capsule_id": "c"}\n'
        "NOT_VALID_JSON\n"
        '{"event_type": "ToolCallCompleted", "agent_id": "a", "tool_name": "read_file", "capsule_id": "c"}\n'
    )
    (capsule_dir / "model-calls.jsonl").write_text(content)

    from novafabric.kg.consumer import CapsuleEventConsumer

    consumer = CapsuleEventConsumer()
    events = _run(_collect(consumer.iter_from_directory(tmp_path)))
    assert len(events) == 2  # bad line skipped


def test_consumer_iter_from_directory_nonexistent(tmp_path: Path) -> None:
    """Non-existent directory yields no events (warning logged)."""
    from novafabric.kg.consumer import CapsuleEventConsumer

    consumer = CapsuleEventConsumer()
    events = _run(_collect(consumer.iter_from_directory(tmp_path / "does_not_exist")))
    assert events == []


# ---------------------------------------------------------------------------
# Local-dir source — zip capsule archives
# ---------------------------------------------------------------------------


def test_consumer_iter_from_zip_capsule(tmp_path: Path) -> None:
    """iter_from_directory yields events from a .zip capsule archive."""
    zip_path = tmp_path / "capsule-zip.zip"
    sample = [
        {"event_type": "ModelCallCompleted", "agent_id": "zip-agent", "model_id": "gpt-4o", "capsule_id": "zip-1"},
    ]
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("model-calls.jsonl", json.dumps(sample[0]))

    from novafabric.kg.consumer import CapsuleEventConsumer

    consumer = CapsuleEventConsumer()
    events = _run(_collect(consumer.iter_from_directory(tmp_path)))
    assert len(events) == 1
    assert events[0]["agent_id"] == "zip-agent"


def test_consumer_bad_zip_is_skipped(tmp_path: Path) -> None:
    """Corrupt zip file is skipped gracefully."""
    bad_zip = tmp_path / "bad.zip"
    bad_zip.write_bytes(b"not a zip file at all")

    from novafabric.kg.consumer import CapsuleEventConsumer

    consumer = CapsuleEventConsumer()
    events = _run(_collect(consumer.iter_from_directory(tmp_path)))
    assert events == []


# ---------------------------------------------------------------------------
# Multiple capsule directories
# ---------------------------------------------------------------------------


def test_consumer_multiple_capsule_dirs(tmp_path: Path) -> None:
    """Events from multiple capsule directories are all yielded."""
    for i in range(3):
        d = tmp_path / f"cap-{i:03d}"
        d.mkdir()
        ev = {"event_type": "ModelCallCompleted", "agent_id": f"agent-{i}",
              "model_id": "gpt-4o", "capsule_id": f"cap-{i}"}
        (d / "model-calls.jsonl").write_text(json.dumps(ev))

    from novafabric.kg.consumer import CapsuleEventConsumer

    consumer = CapsuleEventConsumer()
    events = _run(_collect(consumer.iter_from_directory(tmp_path)))
    assert len(events) == 3
    agent_ids = {e["agent_id"] for e in events}
    assert agent_ids == {"agent-0", "agent-1", "agent-2"}


# ---------------------------------------------------------------------------
# NATS source — unit test (no live NATS)
# ---------------------------------------------------------------------------


def test_consumer_iter_from_nats_no_nats_py_returns_empty(monkeypatch) -> None:
    """iter_from_nats degrades gracefully when nats-py is not installed."""
    import sys
    import unittest.mock as mock

    with mock.patch.dict(sys.modules, {"nats": None}):
        # Force re-import
        if "novafabric.kg.consumer" in sys.modules:
            del sys.modules["novafabric.kg.consumer"]
        from novafabric.kg import consumer as _consumer_mod

        cons = _consumer_mod.CapsuleEventConsumer()
        events = _run(_collect(cons.iter_from_nats("nats://localhost:4222", "nova.test")))
        assert events == []

    # Restore
    if "novafabric.kg.consumer" in sys.modules:
        del sys.modules["novafabric.kg.consumer"]
