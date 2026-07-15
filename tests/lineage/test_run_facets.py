# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for NF-036/037 OpenLineage custom run facets (ADR-0096)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from novafabric.lineage._openlineage import build_events_from_capsule
from novafabric.lineage._run_facets import (
    CAPSULE_FACET_KEY,
    EVAL_FACET_KEY,
    EXEC_PARAMS_FACET_KEY,
    OTEL_CORRELATION_FACET_KEY,
    POLICY_FACET_KEY,
    FacetValidationError,
    build_run_facets,
    capsule_facet,
    eval_facet,
    otel_correlation_facet,
    policy_facet,
)


def _capsule(tmp_path: Path, manifest: dict, eval_result: dict | None = None) -> Path:
    d = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    d.mkdir(parents=True)
    base = {"run_id": "run-036", "command": ["python", "agent.py"], "created_at": "2026-07-04T00:00:00Z",
            "finished_at": "2026-07-04T00:00:01Z", "exit_code": 0}
    base.update(manifest)
    (d / "capsule.yaml").write_text(yaml.dump(base))
    if eval_result is not None:
        (d / "eval_result.json").write_text(json.dumps(eval_result))
    return d


# ── R2: every facet carries _producer + _schemaURL ───────────────────────────


def test_every_facet_has_producer_and_schema_url() -> None:
    facets = [
        capsule_facet("c", "r", "sha256:h"),
        eval_facet("passed"),
        policy_facet("gate-1", "allow"),
        otel_correlation_facet("0" * 32, "0" * 16),
    ]
    for f in facets:
        assert f["_producer"].startswith("https://")
        assert f["_schemaURL"].startswith("https://")


# ── R11: malformed facet fails validation ────────────────────────────────────


def test_invalid_verdict_raises() -> None:
    with pytest.raises(FacetValidationError):
        eval_facet("maybe")


def test_invalid_decision_raises() -> None:
    with pytest.raises(FacetValidationError):
        policy_facet("g", "perhaps")


def test_invalid_trace_id_raises() -> None:
    with pytest.raises(FacetValidationError):
        otel_correlation_facet("too-short", "0" * 16)


# ── build_run_facets over a capsule (R3/R4/R5/R6) ────────────────────────────


def test_capsule_facet_from_capsule(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {"model": "gpt-4o"})
    facets = build_run_facets(cap, yaml.safe_load((cap / "capsule.yaml").read_text()))
    cf = facets[CAPSULE_FACET_KEY]
    assert cf["run_id"] == "run-036"
    assert cf["capsule_id"] == cap.name
    assert cf["capsule_hash"].startswith("sha256:")


def test_eval_verdict_passed_and_failed(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {}, eval_result={"passed": True, "suite_id": "smoke", "metrics": [{"x": 1}]})
    facets = build_run_facets(cap, yaml.safe_load((cap / "capsule.yaml").read_text()))
    assert facets[EVAL_FACET_KEY]["verdict"] == "passed"
    assert facets[EVAL_FACET_KEY]["suite_id"] == "smoke"

    cap2 = _capsule(tmp_path / "b", {}, eval_result={"passed": False})
    facets2 = build_run_facets(cap2, yaml.safe_load((cap2 / "capsule.yaml").read_text()))
    assert facets2[EVAL_FACET_KEY]["verdict"] == "failed"


def test_eval_verdict_na_when_no_eval(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {})
    facets = build_run_facets(cap, yaml.safe_load((cap / "capsule.yaml").read_text()))
    assert facets[EVAL_FACET_KEY]["verdict"] == "n/a"


def test_eval_result_unparseable_is_na(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {})
    (cap / "eval_result.json").write_text("{not json")
    facets = build_run_facets(cap, yaml.safe_load((cap / "capsule.yaml").read_text()))
    assert facets[EVAL_FACET_KEY]["verdict"] == "n/a"


def test_eval_metrics_non_list_omitted(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {}, eval_result={"passed": True, "metrics": "oops"})
    facets = build_run_facets(cap, yaml.safe_load((cap / "capsule.yaml").read_text()))
    assert "metrics" not in facets[EVAL_FACET_KEY]


def test_policy_facet_from_manifest(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {"promotion_gate": "prod-gate", "policy_decision": "deny"})
    facets = build_run_facets(cap, yaml.safe_load((cap / "capsule.yaml").read_text()))
    assert facets[POLICY_FACET_KEY] == {
        "_producer": "https://novafabric.io",
        "_schemaURL": "https://novafabric.dev/schemas/ol/novafabric_policy.json",
        "gate_id": "prod-gate",
        "decision": "deny",
    }


def test_policy_decision_sanitized_to_na(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {"policy_decision": "bogus"})
    facets = build_run_facets(cap, yaml.safe_load((cap / "capsule.yaml").read_text()))
    assert facets[POLICY_FACET_KEY]["decision"] == "n/a"


def test_execution_parameters_facet(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {"model": "gpt-4o", "seed": 42, "temperature": 0.0})
    facets = build_run_facets(cap, yaml.safe_load((cap / "capsule.yaml").read_text()))
    params = {p["name"]: p["value"] for p in facets[EXEC_PARAMS_FACET_KEY]["parameters"]}
    assert params["model"] == "gpt-4o"
    assert params["seed"] == "42"
    assert params["temperature"] == "0.0"


def test_no_exec_params_facet_when_none_present(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {})
    facets = build_run_facets(cap, yaml.safe_load((cap / "capsule.yaml").read_text()))
    assert EXEC_PARAMS_FACET_KEY not in facets


def test_otel_correlation_facet_when_ids_present(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {"trace_id": "a" * 32, "span_id": "b" * 16})
    facets = build_run_facets(
        cap, yaml.safe_load((cap / "capsule.yaml").read_text()), with_otel_correlation=True
    )
    assert facets[OTEL_CORRELATION_FACET_KEY]["trace_id"] == "a" * 32
    assert facets[OTEL_CORRELATION_FACET_KEY]["span_id"] == "b" * 16


def test_otel_correlation_skipped_when_ids_malformed(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {"trace_id": "short", "span_id": "b" * 16})
    facets = build_run_facets(
        cap, yaml.safe_load((cap / "capsule.yaml").read_text()), with_otel_correlation=True
    )
    assert OTEL_CORRELATION_FACET_KEY not in facets


# ── R7: emission is additive ─────────────────────────────────────────────────


def test_default_emission_has_no_custom_facets(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {"model": "gpt-4o"})
    events = build_events_from_capsule(cap)
    complete = events[1]
    assert CAPSULE_FACET_KEY not in complete["run"]["facets"]
    assert "nominalTime" in complete["run"]["facets"]  # core facet unchanged


def test_with_facets_emission_adds_custom_facets(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, {"model": "gpt-4o"}, eval_result={"passed": True})
    events = build_events_from_capsule(cap, with_facets=True)
    facets = events[1]["run"]["facets"]
    assert CAPSULE_FACET_KEY in facets
    assert EVAL_FACET_KEY in facets
    assert POLICY_FACET_KEY in facets
    assert "nominalTime" in facets  # still additive


def test_cli_emit_with_facets(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    cap = _capsule(tmp_path, {"model": "gpt-4o", "trace_id": "c" * 32, "span_id": "d" * 16})
    out = tmp_path / "events.ndjson"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["lineage", "emit-openlineage", str(cap), "--output", str(out),
         "--with-facets", "--otel-correlation"],
    )
    assert result.exit_code == 0, result.output
    lines = [json.loads(x) for x in out.read_text().splitlines() if x.strip()]
    complete = lines[-1]
    facets = complete["run"]["facets"]
    assert facets[CAPSULE_FACET_KEY]["run_id"] == "run-036"
    assert facets[OTEL_CORRELATION_FACET_KEY]["trace_id"] == "c" * 32
