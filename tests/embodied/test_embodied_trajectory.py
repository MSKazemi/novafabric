"""ADR-0162 P2 — NF-310 perception→decision→actuation trajectory chain.

Acceptance criteria (spec §6 "Trajectory chain"):

- a perception→world_model→decision→actuation chain walks ``acyclic`` /
  ``no_broken_parent`` / ``monotonic`` / ``complete``;
- a hop whose ``parent`` resolves to no earlier hop is reported by index
  (``broken_parent``), as are a missing parent after the root, a
  self-reference, a forward reference (cycle), a duplicated output, and a
  child timestamped before its parent;
- a stage stepping backwards is a *warning*, and a fresh perception after an
  actuation (closed loop) is not a regression at all;
- a broken chain is still *recordable* — only the walk reports it;
- order is preserved (never sorted) and the root's ``parent: null`` survives
  serialisation; hops honour the I-2 boundary.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from novafabric.embodied import (
    FACET_NAME,
    EmbodiedFacet,
    InvalidReferenceError,
    InvalidTimestampError,
    RawPayloadRejectedError,
    TrajectoryHop,
    attach_facet,
    build_facet,
    walk_trajectory,
)

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "embodied"


def _d(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _hop(
    stage: str, out: str, parent: str | None, ts: str = "2026-07-13T10:00:00Z"
) -> TrajectoryHop:
    return TrajectoryHop(
        stage=stage,  # type: ignore[arg-type]
        input_digest=_d(f"in:{out}"),
        output_digest=_d(out),
        parent=None if parent is None else _d(parent),
        ts=ts,
    )


def _chain() -> list[TrajectoryHop]:
    return [
        _hop("perception", "det", None, "2026-07-13T10:00:00.100Z"),
        _hop("world_model", "world", "det", "2026-07-13T10:00:00.140Z"),
        _hop("decision", "plan", "world", "2026-07-13T10:00:00.180Z"),
        _hop("actuation", "cmd", "plan", "2026-07-13T10:00:00.200Z"),
    ]


def _codes(hops: list[TrajectoryHop]) -> list[tuple[int | None, str]]:
    return [(f.hop_index, f.code) for f in walk_trajectory(hops).findings]


# ── The intact chain ─────────────────────────────────────────────────────


def test_intact_chain_passes_every_property() -> None:
    report = walk_trajectory(_chain())
    assert report.ok and report.findings == []
    assert (report.acyclic, report.no_broken_parent, report.monotonic, report.complete) == (
        True,
        True,
        True,
        True,
    )
    assert report.hop_count == 4


def test_chain_without_actuation_is_ok_but_not_complete() -> None:
    report = walk_trajectory(_chain()[:3])
    assert report.ok and report.complete is False


def test_closed_loop_perception_after_actuation_is_not_a_regression() -> None:
    hops = [*_chain(), _hop("perception", "det2", "cmd", "2026-07-13T10:00:00.300Z")]
    report = walk_trajectory(hops)
    assert report.ok and report.findings == []


def test_branching_is_allowed_parent_may_be_any_earlier_hop() -> None:
    hops = [*_chain(), _hop("decision", "plan-b", "world", "2026-07-13T10:00:00.250Z")]
    assert walk_trajectory(hops).ok


# ── Defects, each named by hop ───────────────────────────────────────────


def test_broken_parent_is_named_by_hop_index() -> None:
    hops = _chain()
    hops[2] = _hop("decision", "plan", "nobody-recorded-this", "2026-07-13T10:00:00.180Z")
    report = walk_trajectory(hops)
    assert not report.ok and not report.no_broken_parent
    assert (2, "broken_parent") in _codes(hops)
    # The actuation hop's ancestry no longer reaches a perception root.
    assert report.complete is False


def test_second_root_is_a_missing_parent() -> None:
    hops = _chain()
    hops[3] = _hop("actuation", "cmd", None, "2026-07-13T10:00:00.200Z")
    assert _codes(hops) == [(3, "missing_parent")]
    assert walk_trajectory(hops).no_broken_parent is False


def test_first_hop_citing_an_unknown_parent_is_broken() -> None:
    hops = _chain()
    hops[0] = _hop("perception", "det", "upstream", "2026-07-13T10:00:00.100Z")
    assert (0, "broken_parent") in _codes(hops)


def test_self_reference_is_a_cycle() -> None:
    hops = _chain()
    hops[1] = _hop("world_model", "world", "world", "2026-07-13T10:00:00.140Z")
    report = walk_trajectory(hops)
    assert (1, "self_reference") in _codes(hops)
    assert report.acyclic is False


def test_forward_reference_is_a_cycle_and_the_walk_terminates() -> None:
    hops = [
        _hop("perception", "a", None),
        _hop("decision", "b", "c"),
        _hop("actuation", "c", "b"),
    ]
    report = walk_trajectory(hops)
    assert (1, "forward_reference") in _codes(hops)
    assert report.acyclic is False and not report.ok


def test_duplicate_output_is_ambiguous() -> None:
    hops = [*_chain(), _hop("actuation", "cmd", "plan", "2026-07-13T10:00:00.210Z")]
    assert (4, "duplicate_output") in _codes(hops)


def test_child_before_parent_is_not_monotonic() -> None:
    hops = _chain()
    hops[3] = _hop("actuation", "cmd", "plan", "2026-07-13T10:00:00.000Z")
    report = walk_trajectory(hops)
    assert (3, "ts_regression") in _codes(hops)
    assert report.monotonic is False and not report.ok


def test_stage_regression_is_a_warning_not_a_failure() -> None:
    hops = _chain()
    hops.append(_hop("world_model", "world2", "cmd", "2026-07-13T10:00:00.300Z"))
    report = walk_trajectory(hops)
    assert [(f.code, f.severity) for f in report.findings] == [("stage_regression", "warning")]
    assert report.ok


def test_empty_chain_is_not_a_vacuous_pass() -> None:
    report = walk_trajectory([])
    assert not report.ok
    assert [f.code for f in report.findings] == ["empty_chain"]


# ── Recording is separate from verifying ─────────────────────────────────


def test_a_broken_chain_is_still_recordable() -> None:
    hops = _chain()
    hops[2] = _hop("decision", "plan", "ghost")
    facet = build_facet(trajectory=hops)
    assert facet is not None and facet.trajectory is not None
    assert len(facet.trajectory) == 4


def test_order_is_preserved_never_sorted() -> None:
    hops = list(reversed(_chain()))
    facet = build_facet(trajectory=hops)
    assert facet is not None and facet.trajectory == hops


def test_root_parent_null_survives_exclude_none() -> None:
    out = attach_facet({}, build_facet(trajectory=_chain()))
    first = out["facets"][FACET_NAME]["trajectory"][0]
    assert "parent" in first and first["parent"] is None


def test_no_trajectory_writes_no_key() -> None:
    facet = EmbodiedFacet()
    assert "trajectory" not in facet.model_dump(exclude_none=True)


# ── Shape + I-2 boundary ─────────────────────────────────────────────────


def test_unknown_stage_is_refused() -> None:
    with pytest.raises(ValidationError):
        _hop("planning", "x", None)


def test_non_digest_is_refused_by_name() -> None:
    with pytest.raises(InvalidReferenceError):
        TrajectoryHop(
            stage="perception",
            input_digest="frame-0001.png",
            output_digest=_d("x"),
            ts="2026-07-13T10:00:00Z",
        )


def test_frame_bytes_where_a_digest_belongs_are_a_raw_payload() -> None:
    with pytest.raises(RawPayloadRejectedError):
        TrajectoryHop.model_validate(
            {
                "stage": "perception",
                "input_digest": b"\x89PNG....",
                "output_digest": _d("x"),
                "ts": "2026-07-13T10:00:00Z",
            }
        )


def test_point_cloud_extra_on_a_hop_is_refused() -> None:
    with pytest.raises(RawPayloadRejectedError, match="point_cloud"):
        TrajectoryHop.model_validate(
            {
                "stage": "perception",
                "input_digest": _d("i"),
                "output_digest": _d("o"),
                "ts": "2026-07-13T10:00:00Z",
                "point_cloud": [],
            }
        )


def test_naive_hop_timestamp_is_refused() -> None:
    with pytest.raises(InvalidTimestampError):
        _hop("perception", "x", None, "2026-07-13T10:00:00")


def test_walk_results_are_not_stored_in_the_facet() -> None:
    """A stored `no_broken_parent: true` would be a self-attestation."""
    dumped = attach_facet({}, build_facet(trajectory=_chain()))["facets"][FACET_NAME]
    assert {"trajectory_acyclic", "no_broken_parent"}.isdisjoint(dumped["verified"])


# ── Golden fixtures ──────────────────────────────────────────────────────


def test_golden_valid_trajectory_walks_clean() -> None:
    raw: dict[str, Any] = json.loads((FIXTURES / "valid-odd-trajectory-facet.json").read_text())
    facet = EmbodiedFacet.model_validate(raw)
    assert facet.trajectory is not None
    assert [h.stage for h in facet.trajectory] == [
        "perception",
        "world_model",
        "decision",
        "actuation",
    ]
    report = walk_trajectory(facet.trajectory)
    assert report.ok and report.complete


def test_golden_broken_parent_fixture_loads_but_fails_the_walk() -> None:
    raw = json.loads((FIXTURES / "trajectory-broken-parent-facet.json").read_text())
    facet = EmbodiedFacet.model_validate(raw)
    assert facet.trajectory is not None
    report = walk_trajectory(facet.trajectory)
    assert [(f.hop_index, f.code) for f in report.findings] == [(2, "broken_parent")]


def test_non_string_parent_is_a_shape_error() -> None:
    with pytest.raises(ValidationError):
        TrajectoryHop.model_validate(
            {
                "stage": "decision",
                "input_digest": _d("i"),
                "output_digest": _d("o"),
                "parent": 7,
                "ts": "2026-07-13T10:00:00Z",
            }
        )


# ── Linear-time walk and the MAX_HOPS bound (review fix) ──────────────────


def _complete_reference(hops: list[TrajectoryHop]) -> bool:
    """The pre-fix per-hop walk back to the root, kept verbatim as an oracle."""
    earlier: dict[str, int] = {}
    parents: dict[int, int | None] = {}
    for index, hop in enumerate(hops):
        parents[index] = earlier.get(hop.parent) if hop.parent is not None else None
        earlier.setdefault(hop.output_digest, index)

    def reaches(start: int) -> bool:
        current = start
        while (parent_index := parents[current]) is not None:
            current = parent_index
        root = hops[current]
        return root.stage == "perception" and root.parent is None

    return any(h.stage == "actuation" and reaches(i) for i, h in enumerate(hops))


def _walk_variants() -> list[list[TrajectoryHop]]:
    base = _chain()
    closed_loop = [
        *base,
        _hop("perception", "det2", "cmd", "2026-07-13T10:00:01Z"),
        _hop("actuation", "cmd2", "det2", "2026-07-13T10:00:02Z"),
    ]
    variants = [base, base[:3], closed_loop]
    for index, parent in [(1, None), (2, "ghost"), (2, "plan"), (1, "cmd"), (3, "det")]:
        hops = list(base)
        hops[index] = _hop(hops[index].stage, f"v{index}", parent)
        variants.append(hops)
    variants.append([_hop("decision", "plan", None), _hop("actuation", "cmd", "plan")])
    for name in ("valid-odd-trajectory-facet.json", "trajectory-broken-parent-facet.json"):
        facet = EmbodiedFacet.model_validate(json.loads((FIXTURES / name).read_text()))
        assert facet.trajectory is not None
        variants.append(list(facet.trajectory))
    return variants


@pytest.mark.parametrize("hops", _walk_variants())
def test_complete_matches_the_pre_fix_walk(hops: list[TrajectoryHop]) -> None:
    assert walk_trajectory(hops).complete is _complete_reference(hops)


def _long_trajectory(length: int, root_stage: str) -> list[TrajectoryHop]:
    # A non-perception root is the old worst case: no actuation reaches a
    # perception root, so every actuation hop walked the full chain back.
    hops = [_hop(root_stage, "h0", None)]
    hops.extend(_hop("actuation", f"h{i}", f"h{i - 1}") for i in range(1, length))
    return hops


@pytest.mark.parametrize(("root_stage", "complete"), [("decision", False), ("perception", True)])
def test_trajectory_at_the_hop_bound_walks_in_linear_time(root_stage: str, complete: bool) -> None:
    import time

    from novafabric.embodied.trajectory import MAX_HOPS

    hops = _long_trajectory(MAX_HOPS, root_stage)
    started = time.perf_counter()
    report = walk_trajectory(hops)
    elapsed = time.perf_counter() - started
    assert report.hop_count == MAX_HOPS and report.complete is complete
    assert report.ok if complete else report.findings == []
    assert elapsed < 1.0, f"walking {MAX_HOPS} hops took {elapsed:.2f}s"


def test_trajectory_over_the_hop_bound_is_refused_without_walking() -> None:
    from novafabric.embodied.trajectory import MAX_HOPS

    report = walk_trajectory(_long_trajectory(MAX_HOPS + 1, "perception"))
    assert not report.ok
    assert report.hop_count == MAX_HOPS + 1
    assert [(f.hop_index, f.code, f.severity) for f in report.findings] == [
        (MAX_HOPS, "chain_too_long", "error")
    ]
    assert not (report.acyclic or report.no_broken_parent or report.monotonic or report.complete)
