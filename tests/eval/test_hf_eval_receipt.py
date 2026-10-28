"""Evaluation receipts for pinned Hugging Face references (HF-05).

The two experiments are real: ``run_experiment`` runs a stub command once per dataset item through
the capture orchestrator (one Run Capsule each), scores them exact-match, and finalizes. No model,
no network, no Hugging Face client; the Hub references are built offline with a fake Hub client.
"""
from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from novafabric.eval.experiment import Experiment, ExperimentTarget, TargetKind
from novafabric.eval.experiment_compare import compare_experiments
from novafabric.eval.experiment_dataset import load_dataset
from novafabric.eval.experiment_runner import run_experiment
from novafabric.eval.hf_receipt import (
    CARD_BLOCK_BEGIN,
    CARD_BLOCK_END,
    NOT_VERIFIED,
    EvalReceipt,
    EvalReceiptError,
    HarnessRef,
    append_card_backlink,
    build_eval_receipt,
    compare_receipts,
    render_card_backlink,
    render_eval_results_yaml,
    to_eval_results,
    validate_eval_results,
    write_eval_results,
)
from novafabric.eval.significance import wilson_interval
from novafabric.interop.hf_hub import declare_reference, resolve_reference

SHA = "c" * 40
MODEL_SHA_1, MODEL_SHA_2 = "1" * 40, "2" * 40
DIGEST = "sha256:" + "d" * 64
ITEMS = ["alpha", "bravo", "charlie", "delta", "echo", "boom"]


class _Api:
    """The Hub client's call shape, answering a fixed commit."""

    def __init__(self, sha: str, repo_id: str) -> None:
        self.sha, self.repo_id = sha, repo_id

    def _info(self, repo_id: str, **kw: Any) -> Any:
        return SimpleNamespace(
            sha=self.sha, id=self.repo_id, siblings=[],
            last_modified=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

    model_info = dataset_info = space_info = _info


def _resolved(repo_type: str, repo_id: str, sha: str, revision: str | None = "main", **kw: Any) -> Any:
    ref = declare_reference(repo_type, repo_id, revision=revision, **kw)
    return resolve_reference(ref, api=_Api(sha, repo_id), clock=lambda: "2026-10-28T00:00:00Z", environ={})


def _command(variant: str) -> list[str]:
    code = {
        # rev1 echoes every input; rev2 changes one answer and crashes on one item.
        "rev1": "print('{input}')",
        "rev2": (
            "import sys; t='{input}'; sys.exit(1) if t=='boom' else "
            "print(t.upper() if t=='bravo' else t)"
        ),
    }[variant]
    return [sys.executable, "-c", code]


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Experiment]:
    root = tmp_path_factory.mktemp("hf-eval")
    data = root / "items.jsonl"
    data.write_text(
        "\n".join(json.dumps({"item_id": i, "input": i, "expected": i}) for i in ITEMS) + "\n"
    )
    dataset = load_dataset(data)
    out: dict[str, Experiment] = {}
    for variant in ("rev1", "rev2"):
        out[variant] = run_experiment(
            dataset, _command(variant),
            target=ExperimentTarget(kind=TargetKind.AGENT, ref=f"stub@{variant}"),
            runs_dir=root / variant,
        )
    return out


@pytest.fixture
def dataset_ref() -> Any:
    return _resolved("dataset", "acme/qa", SHA, revision=SHA, config="default", split="test")


# ── the receipt only restates what the experiment holds ──────────────────────


def test_counts_and_values_are_the_experiments_own_numbers(runs: dict[str, Experiment], dataset_ref: Any) -> None:
    exp = runs["rev2"]
    receipt = build_eval_receipt(exp, dataset_ref)
    aggregate = exp.aggregate[0]
    (metric,) = receipt.metrics
    assert metric.value == aggregate.value and metric.n == aggregate.n  # copied, not recomputed
    assert metric.interval == aggregate.wilson
    c = receipt.counts
    assert (c.attempted, c.ok, c.errored, c.scored, c.unscored) == (6, 5, 1, 5, 1)
    assert (c.passed, c.failed) == (4, 1) and c.passed + c.failed == c.scored
    assert receipt.experiment_content_hash == exp.content_hash
    assert receipt.target_ref == "stub@rev2"


def test_the_receipt_round_trips_and_does_not_mutate_the_experiment(
    runs: dict[str, Experiment], dataset_ref: Any
) -> None:
    before = runs["rev1"].model_dump_json()
    receipt = build_eval_receipt(runs["rev1"], dataset_ref, harness=HarnessRef(name="stub-harness"))
    assert EvalReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    assert runs["rev1"].model_dump_json() == before


def test_every_receipt_says_it_is_not_a_verified_score(runs: dict[str, Experiment], dataset_ref: Any) -> None:
    receipt = build_eval_receipt(runs["rev1"], dataset_ref)
    assert receipt.claim == NOT_VERIFIED and NOT_VERIFIED in receipt.limitations


def test_a_metric_the_experiment_does_not_hold_cannot_be_receipted(
    runs: dict[str, Experiment], dataset_ref: Any
) -> None:
    with pytest.raises(EvalReceiptError, match="no metric"):
        build_eval_receipt(runs["rev1"], dataset_ref, metric="f1")


def test_only_a_finalized_experiment_with_the_right_reference_types_is_accepted(
    runs: dict[str, Experiment], dataset_ref: Any
) -> None:
    running = runs["rev1"].model_copy(update={"status": "running", "content_hash": None})
    with pytest.raises(EvalReceiptError, match="finalized"):
        build_eval_receipt(running, dataset_ref)
    with pytest.raises(EvalReceiptError, match="repo_type 'dataset'"):
        build_eval_receipt(runs["rev1"], declare_reference("model", "acme/qa"))
    with pytest.raises(EvalReceiptError, match="repo_type 'model'"):
        build_eval_receipt(runs["rev1"], dataset_ref, model=dataset_ref)


@pytest.mark.parametrize("bad", ["sha256:abc", "d" * 64, "sha1:" + "d" * 40])
def test_malformed_digests_are_refused(runs: dict[str, Experiment], dataset_ref: Any, bad: str) -> None:
    with pytest.raises(EvalReceiptError):
        build_eval_receipt(runs["rev1"], dataset_ref, template_digest=bad)
    with pytest.raises(EvalReceiptError):
        build_eval_receipt(runs["rev1"], dataset_ref, evidence_refs=[bad])
    with pytest.raises(EvalReceiptError):
        build_eval_receipt(runs["rev1"], dataset_ref, harness=HarnessRef(name="h", container_digest=bad))


def test_free_text_that_looks_like_a_credential_is_refused(runs: dict[str, Experiment], dataset_ref: Any) -> None:
    token = "hf_" + "Qw3rTy7uIo9pAs5dFg1hJk8lZx2cVb4nMq"
    with pytest.raises(EvalReceiptError, match="credential"):
        build_eval_receipt(runs["rev1"], dataset_ref, model_declared=f"acme/tiny {token}")
    with pytest.raises(EvalReceiptError, match="credential"):
        build_eval_receipt(runs["rev1"], dataset_ref, harness=HarnessRef(name=token))
    with pytest.raises(EvalReceiptError, match="too long"):
        build_eval_receipt(runs["rev1"], dataset_ref, model_declared="m" * 500)


def test_building_and_exporting_never_open_a_connection(
    runs: dict[str, Experiment], dataset_ref: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def deny(*a: Any, **k: Any) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr(socket.socket, "connect", deny)
    receipt = build_eval_receipt(runs["rev1"], dataset_ref)
    entries = to_eval_results(receipt, metric="exact_match", task_id="qa_test")
    write_eval_results(entries, tmp_path / ".eval_results" / "r.yaml")
    append_card_backlink("# card\n", render_card_backlink(receipt))


# ── limitations are computed, and they track what was actually established ───


def _limits(receipt: EvalReceipt) -> str:
    return " | ".join(receipt.limitations)


def test_an_unresolved_dataset_is_disclosed_as_declared_not_confirmed(runs: dict[str, Experiment]) -> None:
    receipt = build_eval_receipt(runs["rev1"], declare_reference("dataset", "acme/qa", revision="main"))
    assert "not resolved against the Hub" in _limits(receipt)


def test_a_moving_branch_is_disclosed_even_when_resolved(runs: dict[str, Experiment]) -> None:
    receipt = build_eval_receipt(runs["rev1"], _resolved("dataset", "acme/qa", SHA, revision="main"))
    assert "moving ref" in _limits(receipt) and "'main'" in _limits(receipt)


def test_a_pinned_resolved_dataset_drops_the_resolution_warnings_but_not_the_rest(
    runs: dict[str, Experiment], dataset_ref: Any
) -> None:
    text = _limits(build_eval_receipt(runs["rev1"], dataset_ref))
    assert "moving ref" not in text and "not resolved" not in text
    assert "not cross-checked" in text and NOT_VERIFIED in text


def test_errored_and_unscored_items_are_disclosed_with_their_count(
    runs: dict[str, Experiment], dataset_ref: Any
) -> None:
    assert "1 of 6 items produced no score" in _limits(build_eval_receipt(runs["rev2"], dataset_ref))
    assert "items produced no score" not in _limits(build_eval_receipt(runs["rev1"], dataset_ref))


def test_a_small_sample_is_called_small(runs: dict[str, Experiment], dataset_ref: Any) -> None:
    assert "Small sample (n=6)" in _limits(build_eval_receipt(runs["rev1"], dataset_ref))


def test_model_and_harness_claims_follow_what_was_supplied(runs: dict[str, Experiment], dataset_ref: Any) -> None:
    bare = _limits(build_eval_receipt(runs["rev1"], dataset_ref))
    assert "No model reference was recorded" in bare and "No evaluation harness" in bare
    declared = _limits(build_eval_receipt(runs["rev1"], dataset_ref, model_declared="acme/tiny"))
    assert "declared, not resolved" in declared
    resolved = _limits(build_eval_receipt(
        runs["rev1"], dataset_ref, model=_resolved("model", "acme/tiny", MODEL_SHA_1),
        harness=HarnessRef(name="stub", container_digest=DIGEST),
    ))
    assert "declared, not resolved" not in resolved and "No evaluation harness" not in resolved
    assert "not pinned by a container digest" not in resolved


# ── .eval_results: validated against the documented shape ────────────────────


def test_the_exported_entry_is_valid_and_carries_only_the_receipts_numbers(
    runs: dict[str, Experiment], dataset_ref: Any
) -> None:
    receipt = build_eval_receipt(runs["rev2"], dataset_ref)
    (entry,) = to_eval_results(receipt, metric="exact_match", task_id="qa_test", on="2026-10-28")
    assert validate_eval_results([entry]) == []
    assert entry["value"] == runs["rev2"].aggregate[0].value
    assert entry["dataset"] == {"id": "acme/qa", "task_id": "qa_test", "revision": SHA}
    assert "verifyToken" not in entry
    assert "not a Hugging Face verified score" in entry["notes"]
    assert "fraction in [0, 1]" in entry["notes"]  # the Hub defines no scale, so ours is stated
    assert runs["rev2"].content_hash in entry["notes"]


def test_the_revision_is_exported_only_when_the_dataset_was_resolved(runs: dict[str, Experiment]) -> None:
    declared = build_eval_receipt(runs["rev1"], declare_reference("dataset", "acme/qa"))
    (entry,) = to_eval_results(declared, metric="exact_match", task_id="qa_test")
    assert "revision" not in entry["dataset"]


def test_a_renamed_dataset_is_exported_under_its_canonical_id(runs: dict[str, Experiment]) -> None:
    ref = resolve_reference(
        declare_reference("dataset", "old/qa", revision=SHA),
        api=_Api(SHA, "new/qa"), environ={}, clock=lambda: "2026-10-28T00:00:00Z",
    )
    (entry,) = to_eval_results(
        build_eval_receipt(runs["rev1"], ref), metric="exact_match", task_id="qa_test"
    )
    assert entry["dataset"]["id"] == "new/qa"


def test_an_optional_source_is_validated_and_never_invented(runs: dict[str, Experiment], dataset_ref: Any) -> None:
    receipt = build_eval_receipt(runs["rev1"], dataset_ref)
    (plain,) = to_eval_results(receipt, metric="exact_match", task_id="qa_test")
    assert "source" not in plain and "date" not in plain
    (with_source,) = to_eval_results(
        receipt, metric="exact_match", task_id="qa_test",
        source_url="https://example.org/evidence", source_name="Eval traces", source_user="acme-eval",
    )
    assert with_source["source"] == {
        "url": "https://example.org/evidence", "name": "Eval traces", "user": "acme-eval"
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_url": "http://example.org/x"},
        {"source_url": "https://user:pass@example.org/x"},
        {"source_url": "https://example.org/a b"},
        {"source_url": "file:///etc/passwd"},
        {"source_url": "https://example.org/x", "source_user": "bad handle!"},
        {"task_id": ""},
        {"task_id": "../x"},
        {"on": "yesterday"},
    ],
)
def test_an_export_that_would_be_invalid_is_refused_not_emitted(
    runs: dict[str, Experiment], dataset_ref: Any, kwargs: dict[str, Any]
) -> None:
    receipt = build_eval_receipt(runs["rev1"], dataset_ref)
    args: dict[str, Any] = {"metric": "exact_match", "task_id": "qa_test", **kwargs}
    with pytest.raises(EvalReceiptError):
        to_eval_results(receipt, **args)


def test_the_validator_follows_the_documented_examples() -> None:
    """Both examples from https://huggingface.co/docs/hub/eval-results (checked 2026-10-28)."""
    minimal = yaml.safe_load("- dataset:\n    id: Idavidrein/gpqa\n    task_id: gpqa_diamond\n  value: 0.412\n")
    assert validate_eval_results(minimal) == []
    full = yaml.safe_load(
        "- dataset:\n    id: cais/hle\n    task_id: default\n    revision: " + SHA + "\n"
        "  value: 20.90\n  verifyToken: abc\n  date: '2025-01-15'\n"
        "  source:\n    url: https://huggingface.co/spaces/SaylorTwift/smollm3-mmlu-pro\n"
        "    name: Eval traces\n    user: SaylorTwift\n  notes: no-tools\n"
    )
    # The only thing this module refuses in the documented example is the verifyToken.
    assert validate_eval_results(full) == ["entry 0: verifyToken must not be set by NovaFabric"]
    del full[0]["verifyToken"]
    assert validate_eval_results(full) == []


@pytest.mark.parametrize(
    ("entries", "needle"),
    [
        ([], "non-empty list"),
        ({"dataset": {}}, "non-empty list"),
        (["x"], "must be a mapping"),
        ([{"dataset": {"id": "a/b", "task_id": "t"}, "value": True}], "finite number"),
        ([{"dataset": {"id": "a/b", "task_id": "t"}, "value": float("nan")}], "finite number"),
        ([{"dataset": {"id": "a/b", "task_id": "t"}, "value": float("inf")}], "finite number"),
        ([{"dataset": {"id": "a/b", "task_id": "t"}, "value": "0.5"}], "finite number"),
        ([{"dataset": {"id": "nodatasetslash", "task_id": "t"}, "value": 1}], "owner/name"),
        ([{"dataset": {"id": "a/b"}, "value": 1}], "task_id"),
        ([{"dataset": {"id": "a/b", "task_id": "t", "revision": "main"}, "value": 1}], "40-hex"),
        ([{"dataset": {"id": "a/b", "task_id": "t", "x": 1}, "value": 1}], "unknown dataset key"),
        ([{"dataset": {"id": "a/b", "task_id": "t"}, "value": 1, "extra": 1}], "unknown key"),
        ([{"dataset": {"id": "a/b", "task_id": "t"}, "value": 1, "source": {"name": "n"}}], "https URL"),
        ([{"dataset": {"id": "a/b", "task_id": "t"}, "value": 1, "notes": "n" * 601}], "notes"),
    ],
)
def test_the_validator_rejects_what_the_documented_shape_forbids(entries: Any, needle: str) -> None:
    assert any(needle in p for p in validate_eval_results(entries)), validate_eval_results(entries)


def test_yaml_round_trips_and_writing_is_local_guarded_and_non_destructive(
    runs: dict[str, Experiment], dataset_ref: Any, tmp_path: Path
) -> None:
    receipt = build_eval_receipt(runs["rev1"], dataset_ref)
    entries = to_eval_results(receipt, metric="exact_match", task_id="qa_test")
    assert yaml.safe_load(render_eval_results_yaml(entries)) == entries

    target = tmp_path / "model-repo" / ".eval_results" / "novafabric.yaml"
    assert write_eval_results(entries, target) == target
    assert yaml.safe_load(target.read_text()) == entries
    with pytest.raises(EvalReceiptError, match="exists"):
        write_eval_results(entries, target)
    write_eval_results(entries, target, overwrite=True)
    for bad in (tmp_path / "x.yaml", tmp_path / ".eval_results" / "x.json", tmp_path / "other" / "x.yaml"):
        with pytest.raises(EvalReceiptError, match=r"\.eval_results"):
            write_eval_results(entries, bad)
    assert not (tmp_path / "x.yaml").exists()


def test_writing_invalid_entries_leaves_no_file(tmp_path: Path) -> None:
    target = tmp_path / ".eval_results" / "x.yaml"
    with pytest.raises(EvalReceiptError):
        write_eval_results([{"dataset": {"id": "a/b"}, "value": 1}], target)
    assert not target.exists()


# ── card backlink: append-only ───────────────────────────────────────────────

CARD = "---\nlicense: apache-2.0\ntags: [a]\n---\n# My model\n\nHand-written text.\n"


def test_the_backlink_is_appended_and_every_original_byte_is_kept(
    runs: dict[str, Experiment], dataset_ref: Any
) -> None:
    section = render_card_backlink(build_eval_receipt(runs["rev2"], dataset_ref))
    out = append_card_backlink(CARD, section)
    assert out.startswith(CARD)  # front matter, licence and prose are byte-for-byte unchanged
    assert out.count(CARD_BLOCK_BEGIN) == 1 and out.endswith(section)
    assert "exact_match" in section and NOT_VERIFIED in section and "1 of 6 items" in section


def test_applying_the_backlink_twice_changes_nothing_and_a_new_one_replaces_only_its_block(
    runs: dict[str, Experiment], dataset_ref: Any
) -> None:
    one = render_card_backlink(build_eval_receipt(runs["rev1"], dataset_ref))
    two = render_card_backlink(build_eval_receipt(runs["rev2"], dataset_ref))
    first = append_card_backlink(CARD, one)
    assert append_card_backlink(first, one) == first
    replaced = append_card_backlink(first + "\n## Later human section\n", two)
    assert "## Later human section" in replaced and replaced.startswith(CARD)
    assert replaced.count(CARD_BLOCK_BEGIN) == 1 and two in replaced and one not in replaced


def test_a_damaged_block_or_a_foreign_section_is_refused(runs: dict[str, Experiment], dataset_ref: Any) -> None:
    section = render_card_backlink(build_eval_receipt(runs["rev1"], dataset_ref))
    with pytest.raises(EvalReceiptError, match="damaged"):
        append_card_backlink(CARD + CARD_BLOCK_BEGIN + "\n", section)
    with pytest.raises(EvalReceiptError, match="render_card_backlink"):
        append_card_backlink(CARD, "## anything I like\n")
    assert CARD_BLOCK_END in section


def test_a_non_https_evidence_link_is_refused(runs: dict[str, Experiment], dataset_ref: Any) -> None:
    receipt = build_eval_receipt(runs["rev1"], dataset_ref)
    with pytest.raises(EvalReceiptError):
        render_card_backlink(receipt, evidence_url="http://example.org")
    assert "https://example.org/e" in render_card_backlink(receipt, evidence_url="https://example.org/e")


# ── the two-run regression, with its uncertainty and its limits ──────────────


def _pair(runs: dict[str, Experiment], dataset_ref: Any, *, resolved_models: bool) -> Any:
    models = (
        (_resolved("model", "acme/tiny", MODEL_SHA_1), _resolved("model", "acme/tiny", MODEL_SHA_2))
        if resolved_models else (None, None)
    )
    base = build_eval_receipt(runs["rev1"], dataset_ref, model=models[0], model_declared="acme/tiny@rev1")
    cand = build_eval_receipt(runs["rev2"], dataset_ref, model=models[1], model_declared="acme/tiny@rev2")
    return base, cand, compare_experiments(runs["rev1"], runs["rev2"], metric="exact_match")


def test_the_comparison_restates_the_existing_statistics_and_computes_nothing_new(
    runs: dict[str, Experiment], dataset_ref: Any
) -> None:
    base, cand, comparison = _pair(runs, dataset_ref, resolved_models=True)
    report = compare_receipts(base, cand, comparison)
    assert report["baseline"]["value"] == 1.0 and report["candidate"]["value"] == 0.8
    assert report["difference"] == pytest.approx(-0.2)
    assert report["regression_significant"] is comparison.is_regression()
    assert report["verdict"] == comparison.significance["sprt"]["verdict"]
    assert report["items_paired"] == 5 and report["items_changed"] == 1  # bravo; boom is unpaired
    assert report["baseline"]["interval"] == list(wilson_interval(6, 6)) or tuple(
        report["baseline"]["interval"]
    ) == wilson_interval(6, 6)
    assert tuple(report["candidate"]["interval"]) == wilson_interval(4, 5)
    assert report["limitations"]["candidate"] == cand.limitations


def test_a_difference_is_never_attributed_to_a_model_revision_that_was_not_established(
    runs: dict[str, Experiment], dataset_ref: Any
) -> None:
    unresolved = compare_receipts(*_pair(runs, dataset_ref, resolved_models=False))
    text = " ".join(unresolved["statement"])
    assert "does not identify why" in text and "'Different' is not 'worse'" in text
    assert "Model revision is not established for: baseline, candidate" in text
    assert "Nothing here attributes" in text
    resolved = " ".join(compare_receipts(*_pair(runs, dataset_ref, resolved_models=True))["statement"])
    assert "not established" not in resolved and "can still change outputs" in resolved


def test_a_comparison_over_other_experiments_is_refused(runs: dict[str, Experiment], dataset_ref: Any) -> None:
    base, cand, comparison = _pair(runs, dataset_ref, resolved_models=False)
    with pytest.raises(EvalReceiptError, match="not the experiments"):
        compare_receipts(cand, base, comparison)
