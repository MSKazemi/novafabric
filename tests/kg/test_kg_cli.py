"""CLI tests for nova kg subcommands.

Tests: nova kg audit, nova kg alias, nova kg entity-queue.
All tests that require kuzu use pytest.importorskip("kuzu").
All Tier-2/3 tests use SQLite (no Postgres required).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.kg import kg_app

runner = CliRunner()


# ---------------------------------------------------------------------------
# nova kg audit
# ---------------------------------------------------------------------------


def test_kg_audit_text_output(tmp_path: Path) -> None:
    """nova kg audit with an initialised store exits 0 and shows health info."""
    pytest.importorskip("kuzu")
    kg_path = str(tmp_path / "audit_test.kuzu")
    result = runner.invoke(kg_app, ["init", "--path", kg_path])
    assert result.exit_code == 0, result.output

    result = runner.invoke(kg_app, ["audit", "--path", kg_path])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()


def test_kg_audit_json_output(tmp_path: Path) -> None:
    """nova kg audit --output json produces valid JSON."""
    pytest.importorskip("kuzu")
    kg_path = str(tmp_path / "audit_json.kuzu")
    runner.invoke(kg_app, ["init", "--path", kg_path])

    result = runner.invoke(kg_app, ["audit", "--path", kg_path, "--output", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "store_health" in data


def test_kg_audit_no_kuzu_exits_1() -> None:
    """nova kg audit exits 1 with clear message when kuzu is not installed."""
    import sys
    import unittest.mock as mock

    with mock.patch.dict(sys.modules, {"kuzu": None}):
        if "novafabric.kg.store" in sys.modules:
            del sys.modules["novafabric.kg.store"]
        result = runner.invoke(kg_app, ["audit", "--path", "fake/path"])
        assert result.exit_code == 1

    if "novafabric.kg.store" in sys.modules:
        del sys.modules["novafabric.kg.store"]


def test_kg_audit_uninitialised_store_exits_gracefully(tmp_path: Path) -> None:
    """nova kg audit on an uninitialised store exits gracefully (may be non-zero)."""
    pytest.importorskip("kuzu")
    kg_path = str(tmp_path / "fresh.kuzu")
    result = runner.invoke(kg_app, ["audit", "--path", kg_path])
    assert result.exit_code in (0, 1)


# ---------------------------------------------------------------------------
# nova kg --help smoke test
# ---------------------------------------------------------------------------


def test_kg_help_includes_subcommands() -> None:
    """nova kg --help lists audit, entity-queue, alias subcommands."""
    result = runner.invoke(kg_app, ["--help"])
    assert result.exit_code == 0
    assert "audit" in result.output
    assert "alias" in result.output
    assert "entity-queue" in result.output or "entity_queue" in result.output


# ---------------------------------------------------------------------------
# nova kg alias register + list (SQLite-backed, no Postgres)
# ---------------------------------------------------------------------------


def test_kg_alias_register_and_list(tmp_path: Path) -> None:
    """nova kg alias register writes to SQLite; alias list reads it back."""
    alias_db = str(tmp_path / "alias.db")

    result = runner.invoke(
        kg_app,
        [
            "alias", "register",
            "gpt-4o-2024-05-13", "gpt-4o",
            "--type", "model",
            "--alias-db", alias_db,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "registered" in result.output.lower()

    result = runner.invoke(
        kg_app,
        ["alias", "list", "gpt-4o", "--alias-db", alias_db],
    )
    assert result.exit_code == 0, result.output
    assert "gpt-4o-2024-05-13" in result.output


def test_kg_alias_list_json(tmp_path: Path) -> None:
    """nova kg alias list --output json returns JSON array."""
    alias_db = str(tmp_path / "alias.db")
    runner.invoke(
        kg_app,
        [
            "alias", "register",
            "myalias", "mycanonical",
            "--type", "agent",
            "--alias-db", alias_db,
        ],
    )
    result = runner.invoke(
        kg_app,
        ["alias", "list", "mycanonical", "--alias-db", alias_db, "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert any(e["alias"] == "myalias" for e in data)


def test_kg_alias_list_empty(tmp_path: Path) -> None:
    """nova kg alias list returns 'No aliases' when nothing registered."""
    alias_db = str(tmp_path / "empty_alias.db")
    result = runner.invoke(
        kg_app,
        ["alias", "list", "nobody", "--alias-db", alias_db],
    )
    assert result.exit_code == 0, result.output
    assert "no aliases" in result.output.lower()


# ---------------------------------------------------------------------------
# nova kg entity-queue list/stats/approve/reject (SQLite-backed)
# ---------------------------------------------------------------------------


def test_kg_entity_queue_list_empty(tmp_path: Path) -> None:
    """nova kg entity-queue list shows 'No pending' on an empty queue."""
    queue_db = str(tmp_path / "q.db")
    result = runner.invoke(
        kg_app,
        ["entity-queue", "list", "--queue-db", queue_db],
    )
    assert result.exit_code == 0, result.output
    assert "no pending" in result.output.lower() or "pending" in result.output.lower()


def test_kg_entity_queue_stats_empty(tmp_path: Path) -> None:
    """nova kg entity-queue stats shows 0/0/0 on an empty queue."""
    queue_db = str(tmp_path / "q.db")
    result = runner.invoke(
        kg_app,
        ["entity-queue", "stats", "--queue-db", queue_db],
    )
    assert result.exit_code == 0, result.output
    assert "0" in result.output


def test_kg_entity_queue_stats_json(tmp_path: Path) -> None:
    """nova kg entity-queue stats --output json returns JSON dict."""
    queue_db = str(tmp_path / "q.db")
    result = runner.invoke(
        kg_app,
        ["entity-queue", "stats", "--queue-db", queue_db, "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert set(data.keys()) >= {"pending", "approved", "rejected"}


def test_kg_entity_queue_list_json(tmp_path: Path) -> None:
    """nova kg entity-queue list --output json shows enqueued item."""
    from novafabric.kg.review_queue import HumanReviewQueueWriter, ReviewItem

    queue_db = tmp_path / "q.db"
    q = HumanReviewQueueWriter(db_path=queue_db)
    q.enqueue(ReviewItem(alias="cli-test-model", entity_type="model"))
    q.close()

    result = runner.invoke(
        kg_app,
        ["entity-queue", "list", "--queue-db", str(queue_db), "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert any(i["alias"] == "cli-test-model" for i in data)


def test_kg_entity_queue_approve_and_reject(tmp_path: Path) -> None:
    """nova kg entity-queue approve/reject update queue status."""
    from novafabric.kg.review_queue import HumanReviewQueueWriter, ReviewItem

    queue_db = tmp_path / "q.db"
    q = HumanReviewQueueWriter(db_path=queue_db)
    item_approve = ReviewItem(alias="to-approve", entity_type="model")
    item_reject = ReviewItem(alias="to-reject", entity_type="model")
    q.enqueue(item_approve)
    q.enqueue(item_reject)
    q.close()

    # approve
    result = runner.invoke(
        kg_app,
        [
            "entity-queue", "approve",
            item_approve.item_id,
            "--canonical", "gpt-4o",
            "--queue-db", str(queue_db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "approved" in result.output.lower()

    # reject
    result = runner.invoke(
        kg_app,
        [
            "entity-queue", "reject",
            item_reject.item_id,
            "--queue-db", str(queue_db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "rejected" in result.output.lower()

    # verify stats
    q2 = HumanReviewQueueWriter(db_path=queue_db)
    s = q2.stats()
    q2.close()
    assert s["approved"] == 1
    assert s["rejected"] == 1
    assert s["pending"] == 0


# ---------------------------------------------------------------------------
# nova kg ingest --all
# ---------------------------------------------------------------------------


def test_kg_ingest_all_empty_capsule_dir(tmp_path: Path) -> None:
    """--all on an empty capsule dir prints zero summary and exits 0."""
    pytest.importorskip("kuzu")
    kg_path = tmp_path / "kg.kuzu"
    caps_dir = tmp_path / "capsules"
    caps_dir.mkdir()
    result = runner.invoke(
        kg_app,
        ["ingest", "--all", "--path", str(kg_path), "--capsule-dir", str(caps_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "0" in result.output  # 0 capsules / 0 events


def test_kg_ingest_all_ingests_capsules(tmp_path: Path) -> None:
    """--all ingests all valid capsule subdirs."""
    pytest.importorskip("kuzu")
    kg_path = tmp_path / "kg.kuzu"
    caps_dir = tmp_path / "capsules"
    c1 = caps_dir / "run-001"
    c1.mkdir(parents=True)
    (c1 / "model-calls.jsonl").write_text(
        '{"event_type":"ModelCallCompleted","agent_id":"a1","model_id":"gpt-4o",'
        '"input_tokens":10,"output_tokens":5}\n',
        encoding="utf-8",
    )
    result = runner.invoke(
        kg_app,
        ["ingest", "--all", "--path", str(kg_path), "--capsule-dir", str(caps_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "1" in result.output  # at least 1 capsule scanned


def test_kg_ingest_all_missing_capsule_dir_exits_1(tmp_path: Path) -> None:
    """--all with a non-existent capsule dir exits 1 with a clear error."""
    result = runner.invoke(
        kg_app,
        ["ingest", "--all", "--capsule-dir", str(tmp_path / "no-such-dir")],
    )
    assert result.exit_code == 1
    assert "Error" in result.output or "not a directory" in result.output.lower()


def test_kg_ingest_all_and_no_capsule_dir_mutually_required(tmp_path: Path) -> None:
    """Passing neither CAPSULE_DIR nor --all exits 1 with a usage message."""
    result = runner.invoke(kg_app, ["ingest"])
    assert result.exit_code == 1
    assert "CAPSULE_DIR" in result.output or "required" in result.output.lower() or "Error" in result.output
