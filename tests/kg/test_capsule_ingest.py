"""One definition of "ingest a capsule directory into the KG" (review eval-adapters-query-10/-11).

-10: ``tool-calls.jsonl`` records (``tool_call_id``/``tool_name``/``parent_span_id``,
no ``event_type`` or ``agent_id``) were dropped at debug level while every caller still
counted them as ingested, so no ``USES_TOOL`` edge ever came from a local capsule.
-11: re-ingesting a capsule (``nova kg ingest --all`` twice, or after ``nova serve``
auto-ingest) doubled every ``call_count``. A capsule's ingest now *replaces* that
capsule's earlier contribution to each edge.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

pytest.importorskip("kuzu")

from novafabric.cli.kg import kg_app  # noqa: E402
from novafabric.kg.capsule_ingest import capsule_identity, ingest_capsule_dir  # noqa: E402
from novafabric.kg.pipeline import KGIngestionPipeline  # noqa: E402
from novafabric.kg.store import KGStore  # noqa: E402

runner = CliRunner()


def _model_call(call_id: str, parent: str = "span-agent") -> dict[str, Any]:
    return {
        "schema_version": "0.1.0",
        "model_call_id": call_id,
        "parent_span_id": parent,
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "started_at": "2026-10-24T00:00:00Z",
    }


def _tool_call(call_id: str, tool: str = "read_file", parent: str = "span-agent") -> dict[str, Any]:
    return {
        "schema_version": "0.1.0",
        "tool_call_id": call_id,
        "parent_span_id": parent,
        "tool_name": tool,
        "started_at": "2026-10-24T00:00:00Z",
        "finished_at": "2026-10-24T00:00:01Z",
        "duration_ms": 1000,
        "status": "ok",
    }


def _capsule(root: Path, run_id: str, *, models: int = 1, tools: int = 2) -> Path:
    cdir = root / run_id
    cdir.mkdir(parents=True)
    (cdir / "capsule.yaml").write_text(yaml.safe_dump({"run_id": run_id}))
    (cdir / "model-calls.jsonl").write_text(
        "".join(json.dumps(_model_call(f"m{i}")) + "\n" for i in range(models))
    )
    (cdir / "tool-calls.jsonl").write_text(
        "".join(json.dumps(_tool_call(f"t{i}")) + "\n" for i in range(tools))
    )
    return cdir


@pytest.fixture()
def store(tmp_path: Path) -> KGStore:
    kg = KGStore(tmp_path / "kg.kuzu")
    kg.init_schema()
    return kg


def _counts(store: KGStore) -> tuple[int, int]:
    (model,) = store.query_agent_models("span-agent")
    (tool,) = store.query_agent_tools("span-agent")
    return int(model["call_count"]), int(tool["call_count"])


# ── -10: tool calls are ingested, and a skipped event is counted as skipped ──


def test_a_capture_shaped_tool_record_yields_a_uses_tool_edge(store: KGStore) -> None:
    pipeline = KGIngestionPipeline(store)
    assert pipeline.ingest_event(_tool_call("t1")) is True
    pipeline.flush_to_store()
    (tool,) = store.query_agent_tools("span-agent")
    assert tool["tool_id"] == "read_file" and tool["call_count"] == 1


def test_ingest_event_reports_an_event_it_skipped(store: KGStore) -> None:
    pipeline = KGIngestionPipeline(store)
    assert pipeline.ingest_event({"schema_version": "0.1.0", "unrelated": 1}) is False
    assert pipeline.ingest_event(_model_call("m1")) is True


def test_a_capsule_ingests_its_model_and_tool_calls(store: KGStore, tmp_path: Path) -> None:
    cdir = _capsule(tmp_path / "runs", "01RUNAAAAAAAAAAAAAAAAAAAAA", models=1, tools=2)
    with (cdir / "tool-calls.jsonl").open("a") as fh:
        fh.write("not json\n[1, 2]\n" + json.dumps({"no": "fields"}) + "\n")
    result = ingest_capsule_dir(KGIngestionPipeline(store), cdir)
    assert (result.ingested, result.skipped) == (3, 3)
    assert result.written == 2  # CALLS + USES_TOOL
    assert _counts(store) == (1, 2)


# ── -11: re-ingest replaces the capsule's contribution ───────────────────────


def test_ingesting_the_same_capsule_twice_leaves_the_counts_unchanged(
    store: KGStore, tmp_path: Path
) -> None:
    cdir = _capsule(tmp_path / "runs", "01RUNAAAAAAAAAAAAAAAAAAAAA")
    ingest_capsule_dir(KGIngestionPipeline(store), cdir)
    ingest_capsule_dir(KGIngestionPipeline(store), cdir)
    assert _counts(store) == (1, 2)


def test_two_capsules_add_up(store: KGStore, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    for run_id in ("01RUNAAAAAAAAAAAAAAAAAAAAA", "01RUNBBBBBBBBBBBBBBBBBBBBB"):
        ingest_capsule_dir(KGIngestionPipeline(store), _capsule(runs, run_id))
    assert _counts(store) == (2, 4)


def test_a_copy_of_a_capsule_under_another_path_is_the_same_capsule(
    store: KGStore, tmp_path: Path
) -> None:
    a = _capsule(tmp_path / "a", "01RUNAAAAAAAAAAAAAAAAAAAAA")
    b = _capsule(tmp_path / "b", "01RUNAAAAAAAAAAAAAAAAAAAAA")
    assert capsule_identity(a) == capsule_identity(b) == "01RUNAAAAAAAAAAAAAAAAAAAAA"
    ingest_capsule_dir(KGIngestionPipeline(store), a)
    ingest_capsule_dir(KGIngestionPipeline(store), b)
    assert _counts(store) == (1, 2)


def test_an_unreadable_event_file_is_reported_not_read(store: KGStore, tmp_path: Path) -> None:
    cdir = _capsule(tmp_path / "runs", "01RUNAAAAAAAAAAAAAAAAAAAAA")
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text(json.dumps(_tool_call("x")) + "\n")
    (cdir / "tool-calls.jsonl").unlink()
    (cdir / "tool-calls.jsonl").symlink_to(elsewhere)
    result = ingest_capsule_dir(KGIngestionPipeline(store), cdir)
    assert result.unreadable == ("tool-calls.jsonl",)
    assert store.query_agent_tools("span-agent") == []


# ── the CLI paths go through it ──────────────────────────────────────────────


def test_nova_kg_ingest_twice_counts_tools_and_does_not_double(tmp_path: Path) -> None:
    cdir = _capsule(tmp_path / "runs", "01RUNAAAAAAAAAAAAAAAAAAAAA")
    kg_path = tmp_path / "cli.kuzu"
    for _ in range(2):
        result = runner.invoke(kg_app, ["ingest", str(cdir), "--path", str(kg_path)])
        assert result.exit_code == 0, result.output
        assert "Ingested 3 events" in result.output
    kg = KGStore(kg_path)
    assert _counts(kg) == (1, 2)


def test_nova_kg_ingest_all_twice_does_not_double(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _capsule(runs, "01RUNAAAAAAAAAAAAAAAAAAAAA")
    kg_path = tmp_path / "all.kuzu"
    args = ["ingest", "--all", "--path", str(kg_path), "--capsule-dir", str(runs)]
    for _ in range(2):
        result = runner.invoke(kg_app, args)
        assert result.exit_code == 0, result.output
    assert _counts(KGStore(kg_path)) == (1, 2)


def test_the_directory_consumer_reads_tool_calls_too(tmp_path: Path) -> None:
    import asyncio

    from novafabric.kg.consumer import CapsuleEventConsumer

    _capsule(tmp_path / "runs", "01RUNAAAAAAAAAAAAAAAAAAAAA", models=1, tools=2)

    async def _collect() -> list[dict[str, Any]]:
        return [e async for e in CapsuleEventConsumer().iter_from_directory(tmp_path / "runs")]

    events = asyncio.run(_collect())
    assert sorted(e.get("tool_call_id") or e.get("model_call_id") for e in events) == [
        "m0", "t0", "t1",
    ]
