"""Golden baseline pins — ADR-0147 D1 / NF-160.

ADR-0147 opens D1 with the reason this exists: *"A drift loop is meaningless
without a fixed reference."* So the tests that matter here are not "can it build
a pin" but the four properties that make a pin worth measuring against:
immutability, binding to sealed bytes, honest re-verification, and fail-open.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.assure.baseline import (
    BaselineError,
    BaselinePin,
    BaselineRun,
    attach_facet,
    baseline_run_from_capsule,
    facet_from_capsule,
    pin_baseline,
    supersede,
    verify_pin,
)

_ROOT = "sha256:" + "a" * 64
_OTHER = "sha256:" + "b" * 64


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "capsule"
    d.mkdir()
    (d / "capsule.yaml").write_text("run_id: run-8f2a\n", encoding="utf-8")
    (d / "outputs.txt").write_text("hello\n", encoding="utf-8")
    return d


@pytest.fixture()
def a_pin() -> BaselinePin:
    return pin_baseline(
        "bl-support-agent-golden-2026Q2",
        [BaselineRun(run_id="run-8f2a", baseline_root=_ROOT)],
        "goal",
        pinned_at="2026-07-01T00:00:00Z",
    )


# ── the MUST fields (spec §5) ────────────────────────────────────────────────


def test_a_pin_carries_every_required_field(a_pin: BaselinePin) -> None:
    payload = a_pin.model_dump(exclude_none=True)
    for field in ("baseline_id", "runs", "criterion", "pinned_at", "immutable"):
        assert field in payload, f"spec §5 requires {field}"
    assert payload["runs"][0]["baseline_root"] == _ROOT
    assert payload["immutable"] is True


@pytest.mark.parametrize("criterion", ["goal", "trajectory", "output-dist", "cost"])
def test_the_c3_criterion_vocabulary_is_accepted(criterion: str) -> None:
    record = pin_baseline(
        "bl", [BaselineRun(run_id="r", baseline_root=_ROOT)], criterion,
        pinned_at="2026-07-01T00:00:00Z",
    )
    assert record.criterion == criterion


def test_a_criterion_outside_the_vocabulary_is_refused() -> None:
    """Free-text criteria would make two pins incomparable without anyone noticing."""
    with pytest.raises(BaselineError, match="unknown criterion"):
        pin_baseline(
            "bl", [BaselineRun(run_id="r", baseline_root=_ROOT)], "vibes",
            pinned_at="2026-07-01T00:00:00Z",
        )


# ── immutability is enforced, not documented ─────────────────────────────────


def test_a_pin_cannot_be_mutated(a_pin: BaselinePin) -> None:
    with pytest.raises(Exception):
        a_pin.baseline_id = "something-else"  # type: ignore[misc]


def test_a_pinned_run_cannot_be_mutated(a_pin: BaselinePin) -> None:
    with pytest.raises(Exception):
        a_pin.runs[0].baseline_root = _OTHER  # type: ignore[misc]


def test_immutable_false_is_not_a_supported_state() -> None:
    with pytest.raises(ValueError, match="immutable by definition"):
        BaselinePin(
            baseline_id="bl",
            runs=[BaselineRun(run_id="r", baseline_root=_ROOT)],
            criterion="goal",
            pinned_at="2026-07-01T00:00:00Z",
            immutable=False,
        )


def test_superseding_leaves_the_previous_pin_untouched(a_pin: BaselinePin) -> None:
    """The point of pinning: a past comparison stays reproducible after the move."""
    before = a_pin.model_dump()

    newer = supersede(
        a_pin,
        [BaselineRun(run_id="run-9c3b", baseline_root=_OTHER)],
        pinned_at="2026-10-01T00:00:00Z",
    )

    assert a_pin.model_dump() == before, "superseding must not edit the old pin"
    assert newer.supersedes == a_pin.baseline_id
    assert newer.runs[0].baseline_root == _OTHER
    assert newer.pinned_at == "2026-10-01T00:00:00Z"


# ── digest fields are a containment boundary ─────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-digest",
        "sha256:short",
        "sha256:" + "A" * 64,          # uppercase is not canonical
        "the model said hello world",   # raw payload in a digest field
    ],
)
def test_a_non_canonical_baseline_root_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        BaselineRun(run_id="r", baseline_root=bad)


def test_an_empty_run_id_is_refused() -> None:
    with pytest.raises(ValueError):
        BaselineRun(run_id="   ", baseline_root=_ROOT)


def test_a_pin_needs_at_least_one_run() -> None:
    with pytest.raises(BaselineError):
        pin_baseline("bl", [], "goal", pinned_at="2026-07-01T00:00:00Z")


# ── binding to sealed bytes, and honest re-verification ──────────────────────


def test_a_pin_binds_the_capsules_real_merkle_root(capsule_dir: Path) -> None:
    run = baseline_run_from_capsule("run-8f2a", capsule_dir)
    assert run.baseline_root.startswith("sha256:")
    assert len(run.baseline_root) == len("sha256:") + 64


def test_verification_passes_on_an_unchanged_capsule(capsule_dir: Path) -> None:
    run = baseline_run_from_capsule("run-8f2a", capsule_dir)
    record = pin_baseline("bl", [run], "goal", pinned_at="2026-07-01T00:00:00Z")

    results = verify_pin(record, {"run-8f2a": capsule_dir})

    assert len(results) == 1
    assert results[0].matches is True
    assert results[0].observed_root == run.baseline_root


def test_verification_detects_a_changed_capsule(capsule_dir: Path) -> None:
    run = baseline_run_from_capsule("run-8f2a", capsule_dir)
    record = pin_baseline("bl", [run], "goal", pinned_at="2026-07-01T00:00:00Z")

    (capsule_dir / "outputs.txt").write_text("tampered\n", encoding="utf-8")
    results = verify_pin(record, {"run-8f2a": capsule_dir})

    assert results[0].matches is False
    assert results[0].pinned_root == run.baseline_root
    assert results[0].observed_root != run.baseline_root


def test_verification_never_rewrites_the_pin_on_mismatch(capsule_dir: Path) -> None:
    """Repairing the record would turn detected corruption into a new baseline."""
    run = baseline_run_from_capsule("run-8f2a", capsule_dir)
    record = pin_baseline("bl", [run], "goal", pinned_at="2026-07-01T00:00:00Z")
    before = record.model_dump()

    (capsule_dir / "outputs.txt").write_text("tampered\n", encoding="utf-8")
    verify_pin(record, {"run-8f2a": capsule_dir})

    assert record.model_dump() == before


def test_a_run_with_no_capsule_supplied_is_skipped_not_failed(
    capsule_dir: Path,
) -> None:
    """A check that could not run did not find corruption."""
    run = baseline_run_from_capsule("run-8f2a", capsule_dir)
    record = pin_baseline("bl", [run], "goal", pinned_at="2026-07-01T00:00:00Z")

    assert verify_pin(record, {}) == []


# ── fail-open and additive ───────────────────────────────────────────────────


def test_a_capsule_with_no_baseline_is_byte_identical() -> None:
    capsule = {"run_id": "r", "facets": {"other": {"x": 1}}}
    original = json.dumps(capsule, sort_keys=True)

    assert attach_facet(capsule, None) == capsule
    assert json.dumps(capsule, sort_keys=True) == original, "input was mutated"


def test_attaching_a_pin_does_not_mutate_the_input(a_pin: BaselinePin) -> None:
    capsule: dict = {"run_id": "r", "facets": {"other": {"x": 1}}}
    out = attach_facet(capsule, a_pin)

    assert "baseline" not in capsule["facets"], "input capsule was mutated"
    assert out["facets"]["other"] == {"x": 1}, "sibling facet was dropped"
    assert out["facets"]["baseline"]["baseline_id"] == a_pin.baseline_id


def test_reading_a_baseline_back_round_trips(a_pin: BaselinePin) -> None:
    capsule = attach_facet({"run_id": "r"}, a_pin)
    assert facet_from_capsule(capsule) == a_pin


def test_a_capsule_without_a_baseline_reads_as_none() -> None:
    assert facet_from_capsule({"run_id": "r"}) is None
    assert facet_from_capsule({"run_id": "r", "facets": {}}) is None
    assert facet_from_capsule({"run_id": "r", "facets": "not-a-dict"}) is None


def test_an_invalid_baseline_facet_is_reported_not_silently_ignored() -> None:
    capsule = {"facets": {"baseline": {"baseline_id": "bl"}}}
    with pytest.raises(BaselineError, match="invalid baseline facet"):
        facet_from_capsule(capsule)
