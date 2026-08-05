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
"""ADR-0101 §NF-018 — counterfactual root-cause search.

Sweeps the NF-019 causal-root candidates (shallowest/earliest-ranked first) with
bounded, zero-token intervention replays until one confirms an outcome flip — the
decisive root cause, replay-proven rather than merely ranked. Every attempt
(confirmed, refuted, or honestly unmappable) is recorded so the search itself is
auditable, and the sweep is always bounded so a capsule with many causal roots can
never make a search run unboundedly long.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.diagnose.verify import (
    HypothesisVerification,
    RootCauseSearch,
    Verdict,
    search_root_cause,
)

runner = CliRunner()

_CHECK_QUEUE_SCRIPT = (
    "import json, os, sys\n"
    "queue = json.loads(open(os.environ['NOVAFABRIC_REPLAY_QUEUE_PATH']).read())\n"
    "sys.exit(1 if any(r.get('error') for r in queue) else 0)\n"
)
_ALWAYS_FAIL_SCRIPT = "import sys\nsys.exit(1)\n"


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "agent.py"
    path.write_text(body)
    return [sys.executable, str(path)]


def _write_capsule(
    root: Path,
    run_id: str,
    *,
    status: str = "failure",
    command: list[str] | None = None,
    trace: list[dict] | None = None,
    tool_calls: list[dict] | None = None,
    model_calls: list[dict] | None = None,
) -> Path:
    cap = root / run_id
    cap.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": status,
        "trace_ref": "trace.jsonl",
        "tool_calls_ref": "tool-calls.jsonl",
        "model_calls_ref": "model-calls.jsonl",
    }
    if command is not None:
        manifest["command"] = command
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    (cap / "trace.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (trace or []))
    )
    (cap / "tool-calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (tool_calls or []))
    )
    (cap / "model-calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (model_calls or []))
    )
    return cap


class TestSearchRootCause:
    def test_confirms_earliest_auto_mappable_root(self, tmp_path: Path) -> None:
        # Two sibling causal roots under "root": an unmappable tool failure
        # (ordinal 0, honestly skipped) and a mappable model failure (ordinal 1)
        # whose correction flips the outcome.
        cap = _write_capsule(
            tmp_path,
            "run-confirm",
            command=_script(tmp_path, _CHECK_QUEUE_SCRIPT),
            trace=[
                {"span_id": "root", "name": "root", "started_at": "2026-07-01T00:00:00Z"}
            ],
            tool_calls=[
                {
                    "span_id": "t1",
                    "parent_span_id": "root",
                    "tool_name": "http_post",
                    "status": "error",
                    "error": "request failed",
                    "started_at": "2026-07-01T00:00:01Z",
                }
            ],
            model_calls=[
                {
                    "span_id": "m1",
                    "parent_span_id": "root",
                    "model": "gpt",
                    "status": "error",
                    "error": "model call rejected",
                    "started_at": "2026-07-01T00:00:02Z",
                }
            ],
        )
        search = search_root_cause(cap, replays_base_dir=tmp_path / "replays")
        assert search.run_id == "run-confirm"
        assert [a.step_id for a in search.attempts] == ["t1", "m1"]
        assert search.attempts[0].verdict is Verdict.INCONCLUSIVE
        assert search.attempts[1].verdict is Verdict.CONFIRMED
        assert search.confirmed is not None
        assert search.confirmed.verdict is Verdict.CONFIRMED
        assert search.confirmed.hypothesis is not None
        assert search.confirmed.hypothesis["step_id"] == "m1"
        assert search.candidates_considered == 2
        assert search.bounded is False
        assert "decisive root cause found" in search.note

    def test_no_candidate_flips_is_honestly_reported(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-norefute",
            command=_script(tmp_path, _ALWAYS_FAIL_SCRIPT),
            trace=[
                {"span_id": "root", "name": "root", "started_at": "2026-07-01T00:00:00Z"}
            ],
            model_calls=[
                {
                    "span_id": "m1",
                    "parent_span_id": "root",
                    "model": "gpt",
                    "status": "error",
                    "error": "boom",
                    "started_at": "2026-07-01T00:00:01Z",
                }
            ],
        )
        search = search_root_cause(cap, replays_base_dir=tmp_path / "replays")
        assert search.confirmed is None
        assert search.attempts[0].verdict is Verdict.REFUTED
        assert search.bounded is False
        assert "exhaustive" in search.note

    def test_status_not_failure_short_circuits(self, tmp_path: Path) -> None:
        cap = _write_capsule(tmp_path, "run-ok", status="success")
        search = search_root_cause(cap)
        assert search.confirmed is None
        assert search.attempts == []
        assert "not a failure" in search.note

    def test_no_root_candidates_is_honestly_reported(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-noerr",
            trace=[
                {"span_id": "root", "name": "root", "started_at": "2026-07-01T00:00:00Z"}
            ],
        )
        search = search_root_cause(cap)
        assert search.confirmed is None
        assert search.attempts == []
        assert "nothing to search" in search.note

    def test_taxonomy_carried_on_attempts(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-taxonomy",
            command=_script(tmp_path, _ALWAYS_FAIL_SCRIPT),
            model_calls=[
                {
                    "span_id": "m1",
                    "model": "gpt",
                    "status": "error",
                    "error": "boom",
                    "started_at": "2026-07-01T00:00:00Z",
                }
            ],
        )
        search = search_root_cause(cap, replays_base_dir=tmp_path / "replays")
        assert search.attempts
        for attempt in search.attempts:
            assert attempt.taxonomy is not None
            assert attempt.as_dict()["taxonomy"] == attempt.taxonomy.value

    def test_as_dict_round_trips(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-dict",
            command=_script(tmp_path, _CHECK_QUEUE_SCRIPT),
            model_calls=[
                {
                    "span_id": "m1",
                    "model": "gpt",
                    "status": "error",
                    "error": "boom",
                    "started_at": "2026-07-01T00:00:00Z",
                }
            ],
        )
        search = search_root_cause(cap, replays_base_dir=tmp_path / "replays")
        d = search.as_dict()
        assert d["run_id"] == "run-dict"
        assert d["confirmed"]["verdict"] == "CONFIRMED"
        assert isinstance(d["attempts"], list)
        assert isinstance(search, RootCauseSearch)


class TestBounding:
    """Loop/bound behavior, isolated from the replay engine via a fake `_verify_step`."""

    def _three_root_capsule(self, tmp_path: Path) -> Path:
        return _write_capsule(
            tmp_path,
            "run-3roots",
            trace=[
                {"span_id": "root", "name": "root", "started_at": "2026-07-01T00:00:00Z"},
                {
                    "span_id": "b",
                    "parent_span_id": "root",
                    "name": "b",
                    "status": "error",
                    "error": "b failed",
                    "started_at": "2026-07-01T00:00:01Z",
                },
                {
                    "span_id": "c",
                    "parent_span_id": "root",
                    "name": "c",
                    "status": "error",
                    "error": "c failed",
                    "started_at": "2026-07-01T00:00:02Z",
                },
                {
                    "span_id": "d",
                    "parent_span_id": "root",
                    "name": "d",
                    "status": "error",
                    "error": "d failed",
                    "started_at": "2026-07-01T00:00:03Z",
                },
            ],
        )

    def _patch_verify_step(self, monkeypatch: pytest.MonkeyPatch, verdict: Verdict) -> None:
        from novafabric.diagnose import verify as verify_mod

        def _fake(capsule_dir, step, model_calls, original_outcome, base_dir):  # noqa: ANN001
            return HypothesisVerification(
                verdict=verdict,
                reason="patched for bounding test",
                hypothesis=step.as_dict(),
                intervention=None,
                original_outcome=original_outcome,
                counterfactual_outcome=None,
                intervened_capsule=None,
                taxonomy=step.taxonomy,
            )

        monkeypatch.setattr(verify_mod, "_verify_step", _fake)

    def test_stops_at_first_confirmed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = self._three_root_capsule(tmp_path)
        self._patch_verify_step(monkeypatch, Verdict.CONFIRMED)
        search = search_root_cause(cap)
        assert search.candidates_considered == 1
        assert len(search.attempts) == 1
        assert search.confirmed is not None
        assert search.bounded is False

    def test_max_interventions_bounds_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = self._three_root_capsule(tmp_path)
        self._patch_verify_step(monkeypatch, Verdict.REFUTED)
        search = search_root_cause(cap, max_interventions=2)
        assert search.candidates_considered == 2
        assert search.confirmed is None
        assert search.bounded is True
        assert "bounded, not exhaustive" in search.note

    def test_max_interventions_floor_clamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = self._three_root_capsule(tmp_path)
        self._patch_verify_step(monkeypatch, Verdict.REFUTED)
        search = search_root_cause(cap, max_interventions=0)
        assert search.candidates_considered == 1  # clamped up to a minimum of 1

    def test_max_interventions_ceiling_clamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = self._three_root_capsule(tmp_path)
        self._patch_verify_step(monkeypatch, Verdict.REFUTED)
        # Only 3 candidates exist, so an absurd max_interventions is clamped to
        # the hard ceiling internally but still can't exceed the candidate count.
        search = search_root_cause(cap, max_interventions=10_000)
        assert search.candidates_considered == 3
        assert search.bounded is False  # exhausted the (small) candidate set


class TestCli:
    def _confirmable_capsule(self, tmp_path: Path, run_id: str) -> Path:
        return _write_capsule(
            tmp_path,
            run_id,
            command=_script(tmp_path, _CHECK_QUEUE_SCRIPT),
            model_calls=[
                {
                    "span_id": "m1",
                    "model": "gpt",
                    "status": "error",
                    "error": "boom",
                    "started_at": "2026-07-01T00:00:00Z",
                }
            ],
        )

    def test_help_shows_search_root_cause_flag(self) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(app, ["diagnose", "--help"])
        assert result.exit_code == 0
        assert_flag_in_help(result, "--search-root-cause")
        assert_flag_in_help(result, "--max-interventions")

    def test_json_output_includes_root_cause_search_block(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        self._confirmable_capsule(tmp_path, "run-cli-rc-json")
        result = runner.invoke(
            app,
            [
                "diagnose",
                "run-cli-rc-json",
                "--capsule-dir",
                str(tmp_path),
                "--search-root-cause",
                "--replay-dir",
                str(tmp_path / "replays"),
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        search = payload["root_cause_search"]
        assert search["confirmed"]["verdict"] == "CONFIRMED"
        assert search["attempts"]

    def test_text_output_prints_search_block(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        self._confirmable_capsule(tmp_path, "run-cli-rc-text")
        result = runner.invoke(
            app,
            [
                "diagnose",
                "run-cli-rc-text",
                "--capsule-dir",
                str(tmp_path),
                "--search-root-cause",
                "--replay-dir",
                str(tmp_path / "replays"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Counterfactual root-cause search" in result.output
        assert "Decisive root cause confirmed" in result.output

    def test_default_output_unchanged_without_flag(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        self._confirmable_capsule(tmp_path, "run-cli-rc-plain")
        result = runner.invoke(
            app,
            [
                "diagnose",
                "run-cli-rc-plain",
                "--capsule-dir",
                str(tmp_path),
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "root_cause_search" not in payload

    def test_max_interventions_requires_search_root_cause(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        self._confirmable_capsule(tmp_path, "run-cli-rc-guard")
        result = runner.invoke(
            app,
            [
                "diagnose",
                "run-cli-rc-guard",
                "--capsule-dir",
                str(tmp_path),
                "--max-interventions",
                "3",
            ],
        )
        assert result.exit_code == 1
        assert_flag_in_help(result, "--max-interventions")

    def test_replay_dir_requires_intervene_or_search(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        self._confirmable_capsule(tmp_path, "run-cli-rc-replaydir-guard")
        result = runner.invoke(
            app,
            [
                "diagnose",
                "run-cli-rc-replaydir-guard",
                "--capsule-dir",
                str(tmp_path),
                "--replay-dir",
                str(tmp_path / "replays"),
            ],
        )
        assert result.exit_code == 1
        assert_flag_in_help(result, "--replay-dir")
