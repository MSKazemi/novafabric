"""Tests for token usage-type accounting (ADR-0132, token-usage-types-v0).

Covers: provider usage-payload extraction (with/without usage types, dict and
SDK-object payloads, malformed payloads), the absent != zero invariant, the
capsule roll-up aggregation, the golden fixture corpus against the graduated
schemas, and the cost-report surfaces (CostFacet.usage, ClickHouse ingest and
report output).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import jsonschema
import pytest

from novafabric.cost import clickhouse_store
from novafabric.cost.interceptor import CostInterceptor
from novafabric.cost.usage_types import (
    NAMED_USAGE_FIELDS,
    sum_usage_totals,
    usage_from_anthropic,
    usage_from_openai,
    usage_totals_from_model_calls,
)

REPO_ROOT = Path(__file__).parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "token-usage-types"
SCHEMAS_DIR = REPO_ROOT / "schemas"


# ---------------------------------------------------------------------------
# Helpers — fake SDK objects
# ---------------------------------------------------------------------------


class _Obj:
    """Plain attribute holder standing in for a non-Pydantic SDK object."""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _PydanticLike(_Obj):
    """SDK object exposing its fields via ``model_dump()`` (Pydantic-style)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fields = kwargs

    def model_dump(self) -> dict[str, Any]:
        return dict(self._fields)


# ---------------------------------------------------------------------------
# Extraction — OpenAI
# ---------------------------------------------------------------------------


class TestUsageFromOpenAI:
    def test_full_breakdown_dict(self) -> None:
        usage = {
            "prompt_tokens": 1024,
            "completion_tokens": 3120,
            "total_tokens": 4144,
            "prompt_tokens_details": {"cached_tokens": 768, "audio_tokens": 12},
            "completion_tokens_details": {"reasoning_tokens": 2560, "audio_tokens": 7},
        }
        block = usage_from_openai(usage)
        assert block == {
            "input_tokens": 1024,
            "output_tokens": 3120,
            "total_tokens": 4144,
            "cached_tokens": 768,
            "audio_input_tokens": 12,
            "reasoning_tokens": 2560,
            "audio_output_tokens": 7,
        }

    def test_unrecognized_detail_tokens_land_in_extra(self) -> None:
        usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "completion_tokens_details": {
                "reasoning_tokens": 3,
                "accepted_prediction_tokens": 2,
                "rejected_prediction_tokens": 1,
            },
        }
        block = usage_from_openai(usage)
        assert block is not None
        assert block["extra"] == {
            "accepted_prediction_tokens": 2,
            "rejected_prediction_tokens": 1,
        }

    def test_unrecognized_toplevel_tokens_land_in_extra(self) -> None:
        block = usage_from_openai({"prompt_tokens": 1, "tool_use_tokens": 9})
        assert block is not None
        assert block["extra"] == {"tool_use_tokens": 9}

    def test_absent_types_stay_absent_not_zero(self) -> None:
        block = usage_from_openai({"prompt_tokens": 10, "completion_tokens": 5})
        assert block == {"input_tokens": 10, "output_tokens": 5}
        assert "cached_tokens" not in block
        assert "reasoning_tokens" not in block

    def test_present_zero_is_recorded(self) -> None:
        usage = {
            "prompt_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
        block = usage_from_openai(usage)
        assert block is not None
        assert block["cached_tokens"] == 0  # provider reported zero — kept

    def test_total_tokens_recorded_verbatim_never_recomputed(self) -> None:
        # Provider total deliberately != sum of parts; recorded as-is.
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 999}
        block = usage_from_openai(usage)
        assert block is not None
        assert block["total_tokens"] == 999

    def test_none_usage_returns_none(self) -> None:
        assert usage_from_openai(None) is None

    def test_empty_usage_returns_none(self) -> None:
        assert usage_from_openai({}) is None

    def test_malformed_values_are_skipped_never_guessed(self) -> None:
        usage = {
            "prompt_tokens": "not-a-number",
            "completion_tokens": 5.5,
            "total_tokens": True,
            "weird_tokens": -3,
            "prompt_tokens_details": "garbage",
        }
        assert usage_from_openai(usage) is None

    def test_plain_object_named_fields(self) -> None:
        usage = _Obj(
            prompt_tokens=200,
            completion_tokens=80,
            total_tokens=280,
            prompt_tokens_details=_Obj(cached_tokens=20),
            completion_tokens_details=_Obj(reasoning_tokens=64),
        )
        block = usage_from_openai(usage)
        assert block == {
            "input_tokens": 200,
            "output_tokens": 80,
            "total_tokens": 280,
            "cached_tokens": 20,
            "reasoning_tokens": 64,
        }

    def test_pydantic_like_object_enumerates_extra(self) -> None:
        usage = _PydanticLike(
            prompt_tokens=10,
            completion_tokens=5,
            video_tokens=100,
        )
        block = usage_from_openai(usage)
        assert block is not None
        assert block["extra"] == {"video_tokens": 100}

    def test_magicmock_payload_never_raises(self) -> None:
        # The existing hook tests use MagicMock responses; every Mock
        # attribute must be treated as "not a reported count".
        block = usage_from_openai(MagicMock(prompt_tokens=10, completion_tokens=5))
        assert block == {"input_tokens": 10, "output_tokens": 5}

    def test_model_dump_raising_is_tolerated(self) -> None:
        class _Explosive(_Obj):
            def model_dump(self) -> dict[str, Any]:
                raise RuntimeError("boom")

        block = usage_from_openai(_Explosive(prompt_tokens=10))
        assert block == {"input_tokens": 10}

    def test_unusable_extra_key_is_dropped(self) -> None:
        # Normalizes to "9bad_tokens" which is not snake_case-with-letter-start.
        block = usage_from_openai({"prompt_tokens": 1, "9bad_tokens": 5})
        assert block == {"input_tokens": 1}

    def test_raising_payload_yields_none_never_propagates(self) -> None:
        class _EvilMapping(dict):  # type: ignore[type-arg]
            def get(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("boom")

        assert usage_from_openai(_EvilMapping(prompt_tokens=1)) is None
        assert usage_from_anthropic(_EvilMapping(input_tokens=1)) is None


# ---------------------------------------------------------------------------
# Extraction — Anthropic
# ---------------------------------------------------------------------------


class TestUsageFromAnthropic:
    def test_cache_read_and_write_dict(self) -> None:
        usage = {
            "input_tokens": 5200,
            "output_tokens": 410,
            "cache_read_input_tokens": 4800,
            "cache_creation_input_tokens": 320,
        }
        assert usage_from_anthropic(usage) == {
            "input_tokens": 5200,
            "output_tokens": 410,
            "cached_tokens": 4800,
            "cache_write_tokens": 320,
        }

    def test_thinking_tokens_map_to_reasoning(self) -> None:
        block = usage_from_anthropic({"output_tokens": 50, "thinking_tokens": 30})
        assert block is not None
        assert block["reasoning_tokens"] == 30
        assert "extra" not in block

    def test_unrecognized_tokens_key_lands_in_extra(self) -> None:
        block = usage_from_anthropic({"input_tokens": 1, "server_tool_use_tokens": 4})
        assert block is not None
        assert block["extra"] == {"server_tool_use_tokens": 4}

    def test_plain_object(self) -> None:
        usage = _Obj(input_tokens=300, output_tokens=150, cache_read_input_tokens=30)
        assert usage_from_anthropic(usage) == {
            "input_tokens": 300,
            "output_tokens": 150,
            "cached_tokens": 30,
        }

    def test_none_and_empty_return_none(self) -> None:
        assert usage_from_anthropic(None) is None
        assert usage_from_anthropic({}) is None


# ---------------------------------------------------------------------------
# Aggregation — sum_usage_totals / usage_totals_from_model_calls
# ---------------------------------------------------------------------------


class TestSumUsageTotals:
    def test_sums_named_fields(self) -> None:
        totals = sum_usage_totals([
            {"input_tokens": 100, "output_tokens": 10, "reasoning_tokens": 8},
            {"input_tokens": 50, "output_tokens": 5},
        ])
        assert totals == {
            "input_tokens": 150,
            "output_tokens": 15,
            "reasoning_tokens": 8,
        }

    def test_absent_fields_are_skipped_not_zeroed(self) -> None:
        totals = sum_usage_totals([
            {"input_tokens": 10},
            {"output_tokens": 5},
        ])
        assert totals == {"input_tokens": 10, "output_tokens": 5}
        # A field nobody reported must be absent in the roll-up too.
        assert "cached_tokens" not in totals

    def test_present_zero_contributes_a_present_total(self) -> None:
        totals = sum_usage_totals([{"cached_tokens": 0}])
        assert totals == {"cached_tokens": 0}

    def test_extra_summed_per_key(self) -> None:
        totals = sum_usage_totals([
            {"extra": {"video_input_tokens": 1000, "tool_use_tokens": 1}},
            {"extra": {"video_input_tokens": 280}},
        ])
        assert totals == {
            "extra": {"video_input_tokens": 1280, "tool_use_tokens": 1}
        }

    def test_no_blocks_yields_none(self) -> None:
        assert sum_usage_totals([]) is None

    def test_invalid_values_and_blocks_are_skipped(self) -> None:
        totals = sum_usage_totals([
            {"input_tokens": -5, "output_tokens": True, "cached_tokens": "9"},
            {"extra": {"bad": -1, "ok_tokens": 2}},
            "not-a-mapping",  # type: ignore[list-item]
        ])
        assert totals == {"extra": {"ok_tokens": 2}}

    def test_field_order_follows_spec(self) -> None:
        totals = sum_usage_totals([
            {"total_tokens": 3, "input_tokens": 1, "output_tokens": 2}
        ])
        assert totals is not None
        assert list(totals) == ["input_tokens", "output_tokens", "total_tokens"]
        assert set(totals) <= set(NAMED_USAGE_FIELDS)


class TestUsageTotalsFromModelCalls:
    def test_rolls_up_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "model-calls.jsonl"
        records = [
            {"model_call_id": "c1", "nova.usage": {"input_tokens": 100, "cached_tokens": 40}},
            {"model_call_id": "c2", "nova.usage": {"input_tokens": 50, "reasoning_tokens": 7}},
            {"model_call_id": "c3"},  # no usage block — skipped, not zeroed
        ]
        with path.open("w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
            fh.write("\n{not json\n")  # blank + malformed lines tolerated
        assert usage_totals_from_model_calls(path) == {
            "input_tokens": 150,
            "cached_tokens": 40,
            "reasoning_tokens": 7,
        }

    def test_missing_file_yields_none(self, tmp_path: Path) -> None:
        assert usage_totals_from_model_calls(tmp_path / "nope.jsonl") is None

    def test_no_usage_blocks_yields_none(self, tmp_path: Path) -> None:
        path = tmp_path / "model-calls.jsonl"
        path.write_text(json.dumps({"model_call_id": "c1"}) + "\n")
        assert usage_totals_from_model_calls(path) is None


# ---------------------------------------------------------------------------
# CostFacet.usage (interceptor surface)
# ---------------------------------------------------------------------------


class TestCostFacetUsage:
    def test_openai_facet_carries_usage_superset(self) -> None:
        resp = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 10},
                "completion_tokens_details": {"reasoning_tokens": 30},
            }
        }
        facet = CostInterceptor.extract_from_openai_response(resp, "o1")
        # Legacy scalars unchanged...
        assert facet.input_tokens == 100
        assert facet.completion_tokens == 50
        assert facet.cached_tokens == 10
        # ...and the superset view must match them when present (spec rule).
        assert facet.usage is not None
        assert facet.usage["input_tokens"] == facet.input_tokens
        assert facet.usage["output_tokens"] == facet.completion_tokens
        assert facet.usage["cached_tokens"] == facet.cached_tokens
        assert facet.usage["reasoning_tokens"] == 30

    def test_anthropic_facet_carries_cache_write(self) -> None:
        resp = {
            "usage": {
                "input_tokens": 120,
                "output_tokens": 60,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 15,
            }
        }
        facet = CostInterceptor.extract_from_anthropic_response(resp, "claude-sonnet-4-6")
        assert facet.usage is not None
        assert facet.usage["cached_tokens"] == 5
        assert facet.usage["cache_write_tokens"] == 15

    def test_absent_usage_stays_none(self) -> None:
        facet = CostInterceptor.extract_from_openai_response({}, "gpt-4o")
        assert facet.usage is None
        facet = CostInterceptor.extract_from_anthropic_response(object(), "claude-3-haiku")
        assert facet.usage is None


# ---------------------------------------------------------------------------
# Capture hooks write nova.usage on the model-call record
# ---------------------------------------------------------------------------


def _model_calls(base: Path, run_id: str) -> list[dict[str, Any]]:
    text = (base / run_id / "model-calls.jsonl").read_text().strip()
    return [json.loads(line) for line in text.splitlines() if line]


class TestHooksRecordNovaUsage:
    RUN_ID = "01HXUSAGE00000000000000000"

    def _writer(self, tmp_path: Path) -> Any:
        from novafabric.capture.capsule import CapsuleWriter

        writer = CapsuleWriter(run_id=self.RUN_ID, base_dir=tmp_path)
        writer.open()
        return writer

    def test_openai_hook_records_usage_block(self, tmp_path: Path) -> None:
        from novafabric.capture.hooks._openai import OpenAIHook

        hook = OpenAIHook(writer=self._writer(tmp_path), parent_span_id="0" * 16)
        fake_response = MagicMock()
        fake_response.model = "o1"
        fake_response.id = "chatcmpl-1"
        fake_response.choices = []
        fake_response.usage = _Obj(
            prompt_tokens=1024,
            completion_tokens=3120,
            total_tokens=4144,
            prompt_tokens_details=_Obj(cached_tokens=768),
            completion_tokens_details=_Obj(reasoning_tokens=2560),
        )
        hook._intercept(MagicMock(return_value=fake_response), model="o1", messages=[])

        record = _model_calls(tmp_path, self.RUN_ID)[0]
        assert record["nova.usage"] == {
            "input_tokens": 1024,
            "output_tokens": 3120,
            "total_tokens": 4144,
            "cached_tokens": 768,
            "reasoning_tokens": 2560,
        }
        # Superset consistency with the legacy scalars.
        assert record["nova.usage"]["input_tokens"] == record["gen_ai.usage.input_tokens"]
        assert record["nova.usage"]["output_tokens"] == record["gen_ai.usage.output_tokens"]

    def test_anthropic_hook_records_usage_block(self, tmp_path: Path) -> None:
        from novafabric.capture.hooks._anthropic import AnthropicHook

        hook = AnthropicHook(writer=self._writer(tmp_path), parent_span_id="0" * 16)
        fake_response = MagicMock()
        fake_response.model = "claude-sonnet-4-6"
        fake_response.id = "msg_1"
        fake_response.content = []
        fake_response.stop_reason = "end_turn"
        fake_response.usage = _Obj(
            input_tokens=5200,
            output_tokens=410,
            cache_read_input_tokens=4800,
            cache_creation_input_tokens=320,
        )
        hook._intercept(
            MagicMock(return_value=fake_response),
            model="claude-sonnet-4-6", messages=[], max_tokens=100,
        )

        record = _model_calls(tmp_path, self.RUN_ID)[0]
        assert record["nova.usage"] == {
            "input_tokens": 5200,
            "output_tokens": 410,
            "cached_tokens": 4800,
            "cache_write_tokens": 320,
        }

    def test_openai_hook_omits_block_when_no_usage(self, tmp_path: Path) -> None:
        from novafabric.capture.hooks._openai import OpenAIHook

        hook = OpenAIHook(writer=self._writer(tmp_path), parent_span_id="0" * 16)
        fake_response = MagicMock()
        fake_response.model = "gpt-4o"
        fake_response.id = "chatcmpl-2"
        fake_response.choices = []
        fake_response.usage = None
        hook._intercept(MagicMock(return_value=fake_response), model="gpt-4o", messages=[])

        record = _model_calls(tmp_path, self.RUN_ID)[0]
        assert "nova.usage" not in record  # absence = today's behavior

    def test_hook_record_with_usage_validates_against_model_call_schema(
        self, tmp_path: Path
    ) -> None:
        from novafabric.capture.hooks._openai import OpenAIHook

        hook = OpenAIHook(writer=self._writer(tmp_path), parent_span_id="0" * 16)
        fake_response = MagicMock()
        fake_response.model = "o1"
        fake_response.id = "chatcmpl-3"
        fake_response.choices = []
        fake_response.usage = _Obj(
            prompt_tokens=10,
            completion_tokens=5,
            completion_tokens_details=_Obj(reasoning_tokens=4),
        )
        hook._intercept(MagicMock(return_value=fake_response), model="o1", messages=[])

        record = _model_calls(tmp_path, self.RUN_ID)[0]
        schema = json.loads(
            (REPO_ROOT / "src/novafabric/schemas/model-call.schema.json").read_text()
        )
        jsonschema.validate(record, schema, format_checker=jsonschema.FormatChecker())


# ---------------------------------------------------------------------------
# Capsule manifest: usage_totals is additive and optional
# ---------------------------------------------------------------------------


class TestManifestUsageTotals:
    def test_capture_without_model_calls_has_no_usage_totals(self, tmp_path: Path) -> None:
        import sys

        import yaml

        from novafabric.capture.orchestrator import CaptureOrchestrator

        script = tmp_path / "agent.py"
        script.write_text("pass\n")
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        result = orch.run(command=[sys.executable, str(script)])
        manifest = yaml.safe_load((result.capsule_dir / "capsule.yaml").read_text())
        assert "usage_totals" not in manifest  # absence unchanged

    @pytest.mark.parametrize(
        "schema_path",
        [
            "src/novafabric/schemas/run-capsule.schema.json",
            "schemas/run-capsule.schema.json",
        ],
    )
    def test_manifest_with_usage_totals_validates(self, schema_path: str) -> None:
        schema = json.loads((REPO_ROOT / schema_path).read_text())
        version = "1.0.0" if "src" not in schema_path else "0.1.0"
        manifest = {
            "schema_version": version,
            "run_id": "01HX0000000000000000000000",
            "created_at": "2026-07-15T00:00:00.000000Z",
            "finished_at": "2026-07-15T00:00:01.000000Z",
            "duration_ms": 1000,
            "status": "success",
            "command": ["python", "agent.py"],
            "capture_mode": "cli-wrapper",
            "novafabric_version": "0.59.0",
            "working_directory": "~/proj",
            "host": {
                "os": "linux",
                "arch": "x86_64",
                "cpu_count": 8,
                "memory_bytes": 8_000_000_000,
                "hostname_redacted": True,
            },
            "environment_ref": "env.lock",
            "replay_policy_ref": "replay.yaml",
            "redaction_proof_ref": "redaction-proof.json",
            "trace_ref": "trace.jsonl",
            "trace_root_span_id": "0" * 16,
            "model_calls_ref": "model-calls.jsonl",
            "tool_calls_ref": "tool-calls.jsonl",
            "assets_ref": "assets.jsonl",
            "inputs": [],
            "outputs": [],
            "model_call_count": 2,
            "tool_call_count": 0,
            "mutating_tool_count": 0,
            "usage_totals": {
                "input_tokens": 150,
                "output_tokens": 15,
                "cached_tokens": 40,
                "reasoning_tokens": 7,
                "extra": {"video_input_tokens": 1280},
            },
        }
        jsonschema.validate(manifest, schema, format_checker=jsonschema.FormatChecker())

    def test_manifest_rejects_unknown_usage_total_field(self) -> None:
        schema = json.loads(
            (REPO_ROOT / "src/novafabric/schemas/run-capsule.schema.json").read_text()
        )
        sub = schema["properties"]["usage_totals"]
        errors = list(
            jsonschema.Draft202012Validator(sub).iter_errors({"made_up_field": 1})
        )
        assert errors  # unnamed types must go through extra


# ---------------------------------------------------------------------------
# Golden fixtures (graduated from design/spec/fixtures/token-usage-types/)
# ---------------------------------------------------------------------------


def _fixture_cases() -> list[tuple[str, str, bool]]:
    cases: list[tuple[str, str, bool]] = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        schema = "usage-totals" if path.name.startswith("rollup") else "token-usage"
        expect_valid = "-valid" in path.name
        cases.append((path.name, schema, expect_valid))
    return cases


class TestGoldenFixtures:
    def test_corpus_is_complete(self) -> None:
        assert len(_fixture_cases()) == 13

    @pytest.mark.parametrize(("name", "schema_name", "expect_valid"), _fixture_cases())
    def test_fixture(self, name: str, schema_name: str, expect_valid: bool) -> None:
        instance = json.loads((FIXTURES_DIR / name).read_text())
        schema = json.loads((SCHEMAS_DIR / f"{schema_name}.schema.json").read_text())
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        errors = list(validator.iter_errors(instance))
        if expect_valid:
            assert errors == [], f"{name}: unexpected errors: {[e.message for e in errors]}"
        else:
            assert errors, f"{name}: expected rejection but schema accepted it"


# ---------------------------------------------------------------------------
# ClickHouse cost surfaces (mocked client — no network)
# ---------------------------------------------------------------------------


class TestClickHouseUsageSurface:
    def test_ingest_reads_cached_tokens_from_nova_usage(self, tmp_path: Path) -> None:
        mcalls = tmp_path / "model-calls.jsonl"
        events = [
            {
                "model_call_id": "c1",
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.system": "openai",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
                "nova.usage": {"input_tokens": 100, "cached_tokens": 40},
            },
            {  # no usage block: cached column falls back to 0 (non-nullable)
                "model_call_id": "c2",
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 5,
            },
        ]
        with mcalls.open("w") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")

        client = MagicMock()
        result = MagicMock()
        result.result_rows = []
        client.query.return_value = result
        with patch.object(clickhouse_store, "_get_client", return_value=client):
            n = clickhouse_store.ingest_capsule("run1", tmp_path)

        assert n == 2
        rows = client.insert.call_args.args[1]
        assert rows[0][8] == 40  # cached_tokens from nova.usage
        assert rows[1][8] == 0

    def test_cost_report_output_includes_cached_tokens(self) -> None:
        totals = [[123, 45, 40, 0.9]]
        by_model = [["gpt-4o", "openai", 100, 50, 40, 0.5, 7]]
        client = MagicMock()

        def query(sql: str, parameters: dict[str, Any]) -> MagicMock:
            res = MagicMock()
            res.result_rows = totals if "AS total_input" in sql else by_model
            return res

        client.query.side_effect = query
        with patch.object(clickhouse_store, "_get_client", return_value=client):
            out = clickhouse_store.cost_report(days=7)

        assert out["totals"]["cached_tokens"] == 40
        assert out["by_model"][0]["cached_tokens"] == 40
        # cached_tokens must be selected in both aggregation queries
        sqls = [call.args[0] for call in client.query.call_args_list]
        assert all("sum(cached_tokens)" in sql for sql in sqls)
