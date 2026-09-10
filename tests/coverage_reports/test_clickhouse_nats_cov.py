"""Coverage tests for ClickHouse/NATS external-service modules.

These modules talk to ClickHouse and NATS, which are not available in the test
environment.  Every external client is mocked (``unittest.mock`` /
``monkeypatch``) so the module logic runs without real network I/O.

Covered modules:
- ``novafabric.cost.clickhouse_store``
- ``novafabric.evidence_fabric.clickhouse_accumulator``
- ``novafabric.evidence_fabric.nats_consumer``
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from novafabric.cost import clickhouse_store
from novafabric.evidence_fabric import clickhouse_accumulator as cha
from novafabric.evidence_fabric import nats_consumer as nc

# ===========================================================================
# Helpers
# ===========================================================================


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _mock_ch_client(query_rows: list[Any] | None = None) -> MagicMock:
    """A MagicMock standing in for a clickhouse_connect client."""
    client = MagicMock()
    result = MagicMock()
    result.result_rows = query_rows or []
    client.query.return_value = result
    client.command.return_value = None
    client.insert.return_value = None
    return client


# ===========================================================================
# cost.clickhouse_store
# ===========================================================================


class TestCostStoreUrlParsing:
    def test_parse_url_full(self) -> None:
        kw = clickhouse_store._parse_url(
            "http://user:pass@ch.example:9000/mydb"
        )
        assert kw == {
            "host": "ch.example",
            "port": 9000,
            "username": "user",
            "password": "pass",
            "database": "mydb",
        }

    def test_parse_url_defaults(self) -> None:
        kw = clickhouse_store._parse_url("http://localhost")
        assert kw["host"] == "localhost"
        assert kw["port"] == 8123
        assert kw["username"] == "default"
        assert kw["password"] == ""
        assert kw["database"] == "nova"


class TestCostStoreGetClient:
    def test_get_client_raises_without_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
        fake_cc = MagicMock()
        with patch.dict("sys.modules", {"clickhouse_connect": fake_cc}):
            with pytest.raises(RuntimeError, match="NOVA_CLICKHOUSE_URL not set"):
                clickhouse_store._get_client()

    def test_get_client_runs_ddl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")
        client = _mock_ch_client()
        fake_cc = MagicMock()
        fake_cc.get_client.return_value = client
        with patch.dict("sys.modules", {"clickhouse_connect": fake_cc}):
            got = clickhouse_store._get_client()
        assert got is client
        # All DDL statements issued via .command()
        assert client.command.call_count == len(clickhouse_store._DDL_STATEMENTS)
        fake_cc.get_client.assert_called_once()


class TestCostStoreEnsureSchema:
    def test_ensure_schema_applies_migrations(self) -> None:
        client = _mock_ch_client()
        with patch.object(
            clickhouse_store, "_get_client", return_value=client
        ):
            clickhouse_store.ensure_schema()
        # Each migration statement applied.
        assert client.command.call_count == len(clickhouse_store._MIGRATIONS)

    def test_ensure_schema_swallows_errors(self) -> None:
        with patch.object(
            clickhouse_store, "_get_client", side_effect=RuntimeError("boom")
        ):
            # Must not raise.
            clickhouse_store.ensure_schema()


class TestCostStoreIngestCapsule:
    def test_returns_zero_when_no_file(self, tmp_path: Any) -> None:
        # No model-calls.jsonl present -> 0, never touches client.
        assert clickhouse_store.ingest_capsule("run1", tmp_path) == 0

    def test_inserts_rows(self, tmp_path: Any) -> None:
        mcalls = tmp_path / "model-calls.jsonl"
        events = [
            {
                "model_call_id": "c1",
                "gen_ai.response.model": "gpt-4o",
                "gen_ai.system": "openai",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
            },
            # Uses request.model fallback + default provider.
            {
                "model_call_id": "c2",
                "gen_ai.request.model": "claude-3",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 5,
            },
            "   ",  # blank line skipped
            "{not json",  # malformed JSON skipped
        ]
        with mcalls.open("w") as fh:
            for e in events:
                fh.write((e if isinstance(e, str) else json.dumps(e)) + "\n")

        client = _mock_ch_client(query_rows=[])  # no existing rows
        with patch.object(clickhouse_store, "_get_client", return_value=client):
            n = clickhouse_store.ingest_capsule("run1", tmp_path, tenant_id="acme")

        assert n == 2
        client.insert.assert_called_once()
        args, kwargs = client.insert.call_args
        assert args[0] == "nova.cost_events"
        rows = args[1]
        assert len(rows) == 2
        assert rows[0][0] == "run1"  # run_id
        assert rows[0][3] == "acme"  # tenant_id
        assert rows[0][4] == "gpt-4o"  # model_id

    def test_skips_existing_model_call_ids(self, tmp_path: Any) -> None:
        mcalls = tmp_path / "model-calls.jsonl"
        with mcalls.open("w") as fh:
            fh.write(json.dumps({"model_call_id": "dup", "gen_ai.request.model": "m"}) + "\n")

        # Existing rows query returns the same call_id -> all skipped.
        client = _mock_ch_client(query_rows=[["dup"]])
        with patch.object(clickhouse_store, "_get_client", return_value=client):
            n = clickhouse_store.ingest_capsule("run1", tmp_path)

        assert n == 0
        client.insert.assert_not_called()

    def test_existing_query_error_is_tolerated(self, tmp_path: Any) -> None:
        mcalls = tmp_path / "model-calls.jsonl"
        with mcalls.open("w") as fh:
            fh.write(json.dumps({"model_call_id": "c1", "gen_ai.request.model": "m"}) + "\n")

        client = _mock_ch_client(query_rows=[])
        client.query.side_effect = RuntimeError("no table yet")
        with patch.object(clickhouse_store, "_get_client", return_value=client):
            n = clickhouse_store.ingest_capsule("run1", tmp_path)

        assert n == 1
        client.insert.assert_called_once()


class TestCostStoreIngestAll:
    def test_returns_empty_when_no_capsules_dir(self, tmp_path: Any) -> None:
        assert clickhouse_store.ingest_all_capsules(tmp_path) == {}

    def test_scans_and_maps_results(self, tmp_path: Any) -> None:
        capsules = tmp_path / "capsules"
        (capsules / "runA").mkdir(parents=True)
        (capsules / "runB").mkdir()
        # A stray file (not a dir) is skipped.
        (capsules / "stray.txt").write_text("x")

        def fake_ingest(run_id: str, capsule_dir: Any, tenant_id: str = "default") -> int:
            if run_id == "runB":
                raise RuntimeError("bad capsule")
            return 3

        with patch.object(clickhouse_store, "ingest_capsule", side_effect=fake_ingest):
            results = clickhouse_store.ingest_all_capsules(tmp_path)

        assert results["runA"] == 3
        assert results["runB"] == -1  # error sentinel
        assert "stray.txt" not in results


class TestCostStoreCostReport:
    def test_cost_report_with_run_id(self) -> None:
        # ADR-0132: totals/by_model rows now include cached_tokens.
        totals = [[123, 45, 12, 0.98765432]]
        by_model = [["gpt-4o", "openai", 100, 50, 12, 0.5, 7]]
        client = MagicMock()

        def query(sql: str, parameters: dict[str, Any]) -> MagicMock:
            res = MagicMock()
            res.result_rows = totals if "sum(input_tokens)  AS total_input" in sql else by_model
            return res

        client.query.side_effect = query
        with patch.object(clickhouse_store, "_get_client", return_value=client):
            out = clickhouse_store.cost_report(run_id="run1", days=14)

        assert out["ok"] is True
        assert out["backend"] == "clickhouse"
        assert out["run_id"] == "run1"
        assert out["totals"]["input_tokens"] == 123
        assert out["totals"]["completion_tokens"] == 45
        assert out["totals"]["cached_tokens"] == 12
        assert out["by_model"][0]["model_id"] == "gpt-4o"
        assert out["by_model"][0]["cached_tokens"] == 12
        assert out["by_model"][0]["calls"] == 7

    def test_cost_report_empty_totals(self) -> None:
        client = _mock_ch_client(query_rows=[])
        with patch.object(clickhouse_store, "_get_client", return_value=client):
            out = clickhouse_store.cost_report()
        assert out["totals"]["cost_usd"] == 0.0
        assert out["by_model"] == []


class TestCostStoreQueryCostReport:
    def test_formats_rows(self) -> None:
        import datetime

        rows = [["gpt-4o", datetime.date(2026, 5, 1), 1.234567, 5000, 2500, 10]]
        client = _mock_ch_client(query_rows=rows)
        with patch.object(clickhouse_store, "_get_client", return_value=client):
            out = clickhouse_store.query_cost_report(tenant_id="acme", since_days=7)
        assert out[0]["model_id"] == "gpt-4o"
        assert out[0]["date"] == "2026-05-01"
        assert out[0]["run_count"] == 10


# ===========================================================================
# evidence_fabric.clickhouse_accumulator
# ===========================================================================


class TestAccumulatorClient:
    def test_uses_injected_client(self) -> None:
        client = _mock_ch_client()
        acc = cha.ClickHouseAccumulator(client=client)
        assert acc._get_client() is client

    def test_lazily_creates_client_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://chhost:9001")
        monkeypatch.setenv("NOVA_CLICKHOUSE_USER", "u")
        monkeypatch.setenv("NOVA_CLICKHOUSE_PASSWORD", "p")
        client = _mock_ch_client()
        with patch.object(cha.clickhouse_connect, "get_client", return_value=client) as gc:
            acc = cha.ClickHouseAccumulator()
            got = acc._get_client()
        assert got is client
        _, kwargs = gc.call_args
        assert kwargs["host"] == "chhost"
        assert kwargs["port"] == 9001
        assert kwargs["username"] == "u"
        assert kwargs["password"] == "p"

    def test_ensure_schema_runs_once(self) -> None:
        client = _mock_ch_client()
        acc = cha.ClickHouseAccumulator(client=client)
        acc._ensure_schema()
        acc._ensure_schema()  # second call short-circuits
        # Two CREATE TABLE statements, issued only once.
        assert client.command.call_count == 2


class TestAccumulatorWrites:
    def test_ingest_edges_empty(self) -> None:
        acc = cha.ClickHouseAccumulator(client=_mock_ch_client())
        assert acc.ingest_edges([]) == 0

    def test_ingest_edges_serializes_dict_meta(self) -> None:
        client = _mock_ch_client()
        acc = cha.ClickHouseAccumulator(client=client)
        edges = [
            {"from_ref": "a", "to_ref": "b", "edge_type": "USES", "meta": {"k": 1}},
            {"from_ref": "c", "to_ref": "d", "edge_type": "USES", "tenant": "t2"},
        ]
        n = acc.ingest_edges(edges)
        assert n == 2
        args, kwargs = client.insert.call_args
        assert args[0] == "nova.lineage_edges"
        rows = args[1]
        assert rows[0][4] == json.dumps({"k": 1})  # meta serialized
        assert rows[0][3] == "default"  # default tenant
        assert rows[1][3] == "t2"

    def test_ingest_events_empty(self) -> None:
        acc = cha.ClickHouseAccumulator(client=_mock_ch_client())
        assert acc.ingest_events([]) == 0

    def test_ingest_events_maps_fields(self) -> None:
        client = _mock_ch_client()
        acc = cha.ClickHouseAccumulator(client=client)
        events = [
            {
                "run_id": "r1",
                "event_type": "model_call",
                "model": "gpt-4o",  # model fallback
                "tool_name": "search",
                "tokens_total": 42,
            }
        ]
        n = acc.ingest_events(events)
        assert n == 1
        args, _ = client.insert.call_args
        assert args[0] == "nova.capsule_events"
        rows = args[1]
        assert rows[0][0] == "r1"
        assert rows[0][2] == "gpt-4o"
        assert rows[0][4] == "default"

    def test_ingest_capsule_events_alias(self) -> None:
        client = _mock_ch_client()
        acc = cha.ClickHouseAccumulator(client=client)
        n = acc.ingest_capsule_events(
            [{"run_id": "r", "event_type": "tool_call"}]
        )
        assert n == 1


class TestAccumulatorAggregate:
    def test_aggregate_cost_report(self) -> None:
        rows = [["gpt-4o", 12, 3400], ["claude", 5, None]]
        client = _mock_ch_client(query_rows=rows)
        acc = cha.ClickHouseAccumulator(client=client)
        out = acc.aggregate_cost_report("2024-01-01T00:00:00")
        assert out[0] == {"model_id": "gpt-4o", "call_count": 12, "tokens": 3400}
        assert out[1]["tokens"] == 0  # None coerced to 0


class TestAccumulatorRequireGuard:
    def test_require_clickhouse_raises_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cha, "_CH_AVAILABLE", False)
        with pytest.raises(ImportError, match="clickhouse-connect is required"):
            cha._require_clickhouse()


# ===========================================================================
# evidence_fabric.nats_consumer
# ===========================================================================


def _make_consumer(accumulator: Any) -> nc.NATSJetStreamConsumer:
    return nc.NATSJetStreamConsumer(
        accumulator=accumulator, batch_size=10, fetch_timeout=0.01
    )


class TestNatsRequireGuard:
    def test_require_nats_raises_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(nc, "_NATS_AVAILABLE", False)
        with pytest.raises(ImportError, match="nats-py is required"):
            nc._require_nats()


class TestNatsInit:
    def test_default_accumulator_is_duckdb(self) -> None:
        consumer = nc.NATSJetStreamConsumer()
        # DuckDBAccumulator created in-memory; just confirm it exists.
        assert consumer._accumulator is not None
        assert consumer.stats == {"processed": 0, "errors": 0, "dead_letter": 0}

    def test_env_config_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_NATS_URL", "nats://x:4222")
        monkeypatch.setenv("NOVA_NATS_STREAM", "s1")
        monkeypatch.setenv("NOVA_NATS_SUBJECT", "sub.>")
        monkeypatch.setenv("NOVA_NATS_CONSUMER", "cons1")
        consumer = _make_consumer(MagicMock())
        assert consumer._nats_url == "nats://x:4222"
        assert consumer._stream_name == "s1"
        assert consumer._subject == "sub.>"
        assert consumer._consumer_name == "cons1"


class TestNatsStartStop:
    def test_start_connects_subscribes_and_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_sub = MagicMock()
        # fetch returns no messages so the drain loop idles harmlessly.
        fake_sub.fetch = AsyncMock(return_value=[])

        fake_js = MagicMock()
        fake_js.subscribe = AsyncMock(return_value=None)
        fake_js.pull_subscribe = AsyncMock(return_value=fake_sub)

        fake_nc = MagicMock()
        fake_nc.jetstream = MagicMock(return_value=fake_js)
        fake_nc.drain = AsyncMock(return_value=None)

        fake_nats = MagicMock()
        fake_nats.connect = AsyncMock(return_value=fake_nc)

        consumer = _make_consumer(MagicMock())

        async def scenario() -> None:
            with patch.object(nc, "nats", fake_nats):
                await consumer.start()
                # Idempotent: a second start returns immediately.
                await consumer.start()
                assert consumer._running is True
                await asyncio.sleep(0.02)  # let the loop spin at least once
                await consumer.stop()

        _run(scenario())

        fake_nats.connect.assert_awaited()
        fake_js.pull_subscribe.assert_awaited_once()
        fake_nc.drain.assert_awaited()
        assert consumer._running is False
        assert consumer._nc is None

    def test_start_tolerates_subscribe_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_sub = MagicMock()
        fake_sub.fetch = AsyncMock(return_value=[])

        fake_js = MagicMock()
        # subscribe raises (consumer may already exist) -> swallowed.
        fake_js.subscribe = AsyncMock(side_effect=RuntimeError("exists"))
        fake_js.pull_subscribe = AsyncMock(return_value=fake_sub)

        fake_nc = MagicMock()
        fake_nc.jetstream = MagicMock(return_value=fake_js)
        fake_nc.drain = AsyncMock(return_value=None)

        fake_nats = MagicMock()
        fake_nats.connect = AsyncMock(return_value=fake_nc)

        consumer = _make_consumer(MagicMock())

        async def scenario() -> None:
            with patch.object(nc, "nats", fake_nats):
                await consumer.start()
                await consumer.stop()

        _run(scenario())
        fake_js.pull_subscribe.assert_awaited_once()


class TestNatsProcessing:
    def test_put_and_process_routes_edges_and_events(self) -> None:
        acc = MagicMock()
        consumer = _make_consumer(acc)

        edge = {"event_type": "lineage_edge", "from_ref": "a", "to_ref": "b"}
        event = {"event_type": "model_call", "run_id": "r1"}

        async def scenario() -> None:
            await consumer._process_raw(
                [json.dumps(edge).encode(), json.dumps(event).encode()]
            )

        _run(scenario())

        acc.ingest_edges.assert_called_once()
        acc.ingest_capsule_events.assert_called_once()
        assert consumer._processed == 2
        assert consumer.dead_letter_count == 0

    def test_malformed_json_goes_to_dead_letter(self) -> None:
        consumer = _make_consumer(MagicMock())

        async def scenario() -> None:
            await consumer._process_raw([b"{not json"])

        _run(scenario())
        assert consumer.dead_letter_count == 1
        assert consumer._dead_letter[0]["_error"] == "json_decode_error"

    def test_ingest_edges_failure_records_errors(self) -> None:
        acc = MagicMock()
        acc.ingest_edges.side_effect = RuntimeError("ch down")
        consumer = _make_consumer(acc)
        edge = {"event_type": "lineage_edge", "from_ref": "a", "to_ref": "b"}

        async def scenario() -> None:
            await consumer._process_raw([json.dumps(edge).encode()])

        _run(scenario())
        assert consumer._errors == 1
        assert consumer.dead_letter_count >= 1

    def test_ingest_events_failure_records_errors(self) -> None:
        acc = MagicMock()
        acc.ingest_capsule_events.side_effect = RuntimeError("ch down")
        consumer = _make_consumer(acc)
        event = {"event_type": "model_call", "run_id": "r1"}

        async def scenario() -> None:
            await consumer._process_raw([json.dumps(event).encode()])

        _run(scenario())
        assert consumer._errors == 1
        assert consumer.dead_letter_count >= 1

    def test_stop_drains_injected_queue(self) -> None:
        acc = MagicMock()
        consumer = _make_consumer(acc)

        async def scenario() -> None:
            await consumer.put(json.dumps({"event_type": "x", "run_id": "r"}).encode())
            # stop() with no running task still drains the injection queue.
            await consumer.stop()

        _run(scenario())
        acc.ingest_capsule_events.assert_called_once()

    def test_drain_loop_processes_injected_and_fetched(self) -> None:
        acc = MagicMock()
        consumer = _make_consumer(acc)

        # Fake a subscription whose fetch yields one message then raises to idle.
        fetched_msg = MagicMock()
        fetched_msg.data = json.dumps({"event_type": "model_call", "run_id": "r2"}).encode()
        fetched_msg.ack = AsyncMock(return_value=None)

        calls = {"n": 0}

        async def fake_fetch(batch: int, timeout: float) -> list[Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                return [fetched_msg]
            raise RuntimeError("timeout")  # subsequent fetches "time out"

        fake_sub = MagicMock()
        fake_sub.fetch = fake_fetch
        consumer._sub = fake_sub
        consumer._running = True

        async def scenario() -> None:
            await consumer.put(
                json.dumps({"event_type": "lineage_edge", "from_ref": "a", "to_ref": "b"}).encode()
            )
            task = asyncio.create_task(consumer._drain_loop())
            await asyncio.sleep(0.08)
            consumer._running = False
            await asyncio.sleep(0.08)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _run(scenario())
        # Injected edge + fetched event both processed.
        fetched_msg.ack.assert_awaited()
        acc.ingest_edges.assert_called()
        acc.ingest_capsule_events.assert_called()


class TestNatsDeadLetterBound:
    def test_dead_letter_is_bounded(self) -> None:
        consumer = _make_consumer(MagicMock())
        # Fill to the max.
        consumer._dead_letter = [{"x": i} for i in range(nc._DEAD_LETTER_MAX)]
        consumer._add_to_dead_letter([{"y": 1}])
        # Full -> discarded, count unchanged.
        assert consumer.dead_letter_count == nc._DEAD_LETTER_MAX

    def test_dead_letter_partial_overflow(self) -> None:
        consumer = _make_consumer(MagicMock())
        consumer._dead_letter = [{"x": i} for i in range(nc._DEAD_LETTER_MAX - 1)]
        consumer._add_to_dead_letter([{"y": 1}, {"y": 2}, {"y": 3}])
        # Only one slot left -> exactly one taken.
        assert consumer.dead_letter_count == nc._DEAD_LETTER_MAX
