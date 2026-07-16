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

"""Tests for the Inspect-AI eval-log interop bridge (NF-024, ADR-0108)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from novafabric.eval.inspect_interop import (
    IMPORT_RECORD_PATH,
    INSPECT_MAPPING_VERSION,
    InspectLogError,
    export_inspect_log,
    import_inspect_log,
)
from novafabric.eval.scores import SCORES_FILENAME, ScoreSource, ScoreValueType, write_scores

FIXTURES = Path(__file__).parent.parent / "fixtures"
VALID_LOG = FIXTURES / "inspect_log_valid.json"
INVALID_LOG = FIXTURES / "inspect_log_invalid.json"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


# ── import: success ───────────────────────────────────────────────────────────


def test_import_maps_sample_and_aggregate_scores() -> None:
    result = import_inspect_log(VALID_LOG)
    span = [s for s in result.scores if s.subject_kind == "span"]
    agg = [s for s in result.scores if s.subject_kind == "capsule"]
    # 2 samples × (match + model_graded_qa); custom_struct_scorer is unmappable.
    assert len(span) == 4
    # match: accuracy + stderr; model_graded_qa: accuracy.
    assert len(agg) == 3
    assert {s.name for s in agg} == {"match/accuracy", "match/stderr", "model_graded_qa/accuracy"}


def test_import_value_types_are_honest() -> None:
    result = import_inspect_log(VALID_LOG)
    match = [s for s in result.scores if s.name == "match"]
    assert {s.value for s in match} == {"C", "I"}
    assert all(s.value_type is ScoreValueType.CATEGORICAL for s in match)
    graded = [s for s in result.scores if s.name == "model_graded_qa"]
    assert {s.value for s in graded} == {1.0, 0.5}
    assert all(s.value_type is ScoreValueType.NUMERIC for s in graded)


def test_import_provenance_stamped() -> None:
    result = import_inspect_log(VALID_LOG)
    prov = result.provenance
    assert prov.source == "inspect-ai"
    assert prov.mapping_version == INSPECT_MAPPING_VERSION
    assert prov.log_version == 2
    assert prov.task == "example/hello"
    assert prov.model == "openai/gpt-4o"
    assert prov.dataset_name == "hello-dataset"
    for score in result.scores:
        assert score.evaluator_id.startswith("inspect-ai:")
        assert _SHA256_RE.match(score.subject)
        assert _SHA256_RE.match(score.eval_card_digest)


def test_import_judge_vs_code_source() -> None:
    result = import_inspect_log(VALID_LOG)
    by_name = {(s.name, s.subject_kind): s for s in result.scores}
    assert by_name[("match", "span")].source is ScoreSource.CODE
    assert by_name[("model_graded_qa", "span")].source is ScoreSource.JUDGE


def test_import_same_sample_scores_share_subject() -> None:
    result = import_inspect_log(VALID_LOG)
    span = [s for s in result.scores if s.subject_kind == "span"]
    subjects = {s.subject for s in span}
    assert len(subjects) == 2  # one per sample, shared across scorers


# ── import: honesty (unmapped / omitted) ─────────────────────────────────────


def test_import_unmapped_fields_enumerated_not_dropped() -> None:
    result = import_inspect_log(VALID_LOG)
    # structured scorer value has no Score target → preserved verbatim
    assert result.unmapped["samples[1].scores.custom_struct_scorer.value"] == {
        "precision": 0.4,
        "recall": 0.6,
    }
    # eval-header fields without a native target are preserved
    assert "eval.revision" in result.unmapped
    assert "eval.packages" in result.unmapped
    # scorer answer/explanation extras are preserved
    assert result.unmapped["samples[0].scores.model_graded_qa"]["answer"] == "Paris"
    # sample metadata has no Score target
    assert result.unmapped["samples[0].metadata"] == {"category": "geography"}


def test_import_content_fields_omitted_by_name() -> None:
    result = import_inspect_log(VALID_LOG)
    assert "samples[0].input" in result.omitted
    assert "samples[0].output" in result.omitted
    assert "samples[1].target" in result.omitted
    # prompt/response bytes are never copied into the result
    dumped = result.model_dump_json()
    assert "capital of France" not in dumped


# ── import: failure ──────────────────────────────────────────────────────────


def test_import_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        import_inspect_log(FIXTURES / "no-such-inspect-log.json")


def test_import_invalid_log_structure() -> None:
    with pytest.raises(InspectLogError):
        import_inspect_log(INVALID_LOG)


def test_import_not_json(tmp_path: Path) -> None:
    bad = tmp_path / "log.json"
    bad.write_text("not json at all {")
    with pytest.raises(InspectLogError, match="not valid JSON"):
        import_inspect_log(bad)


def test_import_non_object_top_level(tmp_path: Path) -> None:
    bad = tmp_path / "log.json"
    bad.write_text("[1, 2, 3]")
    with pytest.raises(InspectLogError, match="JSON object"):
        import_inspect_log(bad)


def test_import_unsupported_version_named(tmp_path: Path) -> None:
    bad = tmp_path / "log.json"
    bad.write_text(json.dumps({"version": 99, "eval": {"task": "t"}, "samples": []}))
    with pytest.raises(InspectLogError, match="unsupported Inspect log version 99"):
        import_inspect_log(bad)


def test_import_missing_version(tmp_path: Path) -> None:
    bad = tmp_path / "log.json"
    bad.write_text(json.dumps({"eval": {"task": "t"}, "samples": []}))
    with pytest.raises(InspectLogError, match="version"):
        import_inspect_log(bad)


def test_import_boolean_score_value(tmp_path: Path) -> None:
    log = tmp_path / "log.json"
    log.write_text(
        json.dumps(
            {
                "version": 2,
                "eval": {"task": "t"},
                "samples": [{"id": 1, "epoch": 1, "scores": {"passes": {"value": True}}}],
            }
        )
    )
    result = import_inspect_log(log)
    assert len(result.scores) == 1
    assert result.scores[0].value is True
    assert result.scores[0].value_type is ScoreValueType.BOOLEAN


def test_import_samples_not_a_list(tmp_path: Path) -> None:
    bad = tmp_path / "log.json"
    bad.write_text(json.dumps({"version": 2, "eval": {"task": "t"}, "samples": {}}))
    with pytest.raises(InspectLogError, match="'samples' must be a list"):
        import_inspect_log(bad)


def test_import_degenerate_entries_preserved_not_fatal(tmp_path: Path) -> None:
    """Malformed samples/scorer entries land in unmapped instead of crashing."""
    log = tmp_path / "log.json"
    log.write_text(
        json.dumps(
            {
                "version": 2,
                "eval": {"task": "t"},
                "samples": [
                    "not-a-dict",
                    {"id": 1, "epoch": 1, "scores": {"": {"value": 1}}},
                ],
                "results": {
                    "scores": [
                        "not-a-dict",
                        {"name": "", "scorer": "", "metrics": {}},
                        {"name": "m", "scorer": "m", "metrics": "not-a-dict"},
                        {
                            "name": "cat",
                            "scorer": "cat",
                            "metrics": {"verdict": {"name": "verdict", "value": "C"}},
                        },
                    ]
                },
            }
        )
    )
    result = import_inspect_log(log)
    assert result.scores == []
    assert result.unmapped["samples[0]"] == "not-a-dict"
    assert result.unmapped["samples[1].scores."] == {"value": 1}
    assert result.unmapped["results.scores[0]"] == "not-a-dict"
    assert "results.scores[1]" in result.unmapped
    # non-numeric aggregate metric has no capsule-Score target → preserved
    assert "results.scores.cat.metrics.verdict" in result.unmapped


# ── export ───────────────────────────────────────────────────────────────────


def _imported_capsule(tmp_path: Path) -> Path:
    """A capsule populated from the valid fixture (import → write scores + record)."""
    result = import_inspect_log(VALID_LOG)
    cap = tmp_path / "capsule"
    cap.mkdir()
    write_scores(cap / SCORES_FILENAME, result.scores)
    record = cap / IMPORT_RECORD_PATH
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(result.model_dump(exclude={"scores"}), default=str))
    return cap


def test_export_missing_capsule(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        export_inspect_log(tmp_path / "nope")


def test_export_capsule_without_scores_is_valid_empty_log(tmp_path: Path) -> None:
    cap = tmp_path / "empty-capsule"
    cap.mkdir()
    log = export_inspect_log(cap)
    assert log["version"] == 2
    assert log["samples"] == []
    assert log["results"]["total_samples"] == 0
    assert log["results"]["scores"] == []
    assert log["eval"]["task"] == "novafabric/capsule"


def test_export_groups_scores_into_samples(tmp_path: Path) -> None:
    cap = _imported_capsule(tmp_path)
    log = export_inspect_log(cap)
    assert len(log["samples"]) == 2
    first = log["samples"][0]["scores"]
    assert first["match"]["value"] == "C"
    assert first["model_graded_qa"]["value"] == 1.0
    meta = first["match"]["metadata"]
    assert meta["dev.novafabric.source"] == "code"
    assert _SHA256_RE.match(meta["dev.novafabric.eval_card_digest"])


def test_export_aggregates_numeric_mean(tmp_path: Path) -> None:
    cap = _imported_capsule(tmp_path)
    log = export_inspect_log(cap)
    by_name = {e["name"]: e for e in log["results"]["scores"]}
    assert by_name["model_graded_qa"]["metrics"]["mean"]["value"] == pytest.approx(0.75)
    assert by_name["match"]["metrics"]["count"]["value"] == 2
    # imported capsule-level aggregates pass through
    assert by_name["match/accuracy"]["metrics"]["accuracy"]["value"] == 0.5


def test_export_aggregates_boolean_accuracy(tmp_path: Path) -> None:
    from novafabric.eval.scores import Score

    cap = tmp_path / "bool-capsule"
    cap.mkdir()
    digest = "sha256:" + "0" * 64
    scores = [
        Score(
            subject="sha256:" + f"{i}" * 64,
            name="exact_match",
            value=(i == 1),
            value_type=ScoreValueType.BOOLEAN,
            source=ScoreSource.CODE,
            evaluator_id="inspect-ai:exact_match",
            eval_card_digest=digest,
        )
        for i in (1, 2)
    ]
    write_scores(cap / SCORES_FILENAME, scores)
    log = export_inspect_log(cap)
    entry = log["results"]["scores"][0]
    assert entry["metrics"]["accuracy"]["value"] == 0.5


def test_export_ignores_corrupt_import_record(tmp_path: Path) -> None:
    cap = tmp_path / "capsule"
    cap.mkdir()
    record = cap / IMPORT_RECORD_PATH
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{corrupt")
    log = export_inspect_log(cap)
    assert log["eval"]["task"] == "novafabric/capsule"  # falls back to defaults


def test_roundtrip_preserves_header_and_scorer_results(tmp_path: Path) -> None:
    cap = _imported_capsule(tmp_path)
    log = export_inspect_log(cap)
    original = json.loads(VALID_LOG.read_text())
    # Inspect-native header portion restored from the org.inspect record
    assert log["version"] == original["version"]
    assert log["eval"]["task"] == original["eval"]["task"]
    assert log["eval"]["task_id"] == original["eval"]["task_id"]
    assert log["eval"]["run_id"] == original["eval"]["run_id"]
    assert log["eval"]["model"] == original["eval"]["model"]
    assert log["eval"]["dataset"]["name"] == original["eval"]["dataset"]["name"]
    # scorer names and per-sample values survive
    for orig_sample, out_sample in zip(original["samples"], log["samples"]):
        for scorer in ("match", "model_graded_qa"):
            orig_value = orig_sample["scores"][scorer]["value"]
            out_value = out_sample["scores"][scorer]["value"]
            assert out_value == (float(orig_value) if isinstance(orig_value, int) else orig_value)
    # the exported dict is JSON-serializable end to end
    json.dumps(log)
