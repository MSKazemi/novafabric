"""NF-155 behavioral fingerprint (ADR-0147 D5).

The three properties the fingerprint stands on — deterministic, stable across benign
non-determinism, version-sensitive — each fail *quietly* when broken, so each is tested rather
than inspected. The fourth thing worth pinning is that ``distance`` is a real metric computed
over the basis, not a dressed-up digest comparison.
"""

from __future__ import annotations

import pytest

from novafabric.drift import fingerprint as fp_mod
from novafabric.drift.fingerprint import (
    BASIS_SCORE_PROFILE,
    BASIS_TOOL_MIX,
    BASIS_TRAJECTORY,
    FACET_NAME,
    BehavioralFingerprint,
    FingerprintError,
    IncomparableFingerprintsError,
    attach_facet,
    compare_fingerprints,
    facet_from_capsule,
    fingerprint_run,
)
from novafabric.replay.equivalence.canonicalize import (
    RULE_COLLAPSE_IDEMPOTENT_RETRIES,
    RULE_NORMALIZE_ARGUMENTS,
    RULE_REORDER_COMMUTABLE,
    ToolCall,
)

CALLS = [
    ToolCall(name="search", arguments={"q": "novafabric", "k": 5}),
    ToolCall(name="read", arguments={"path": "a.txt"}),
    ToolCall(name="write", arguments={"path": "out.txt"}),
]


def _fp(run_id: str = "run-1", **kwargs: object) -> BehavioralFingerprint:
    return fingerprint_run(run_id, CALLS, **kwargs)  # type: ignore[arg-type]


# ── It reuses the C3 canonicalizer, rather than normalizing a second way ──


def test_fingerprint_calls_the_c3_canonicalizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """D3's rule is one canonicalizer, many consumers — a second one would drift from it."""
    seen: list[dict[str, object]] = []
    real = fp_mod.canonicalize

    def spy(calls, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(dict(kwargs))
        return real(calls, **kwargs)

    monkeypatch.setattr(fp_mod, "canonicalize", spy)
    fingerprint_run("run-1", CALLS, commutable=["read"], idempotent=["search"])

    assert len(seen) == 1
    assert set(seen[0]["commutable"]) == {"read"}  # type: ignore[arg-type]
    assert set(seen[0]["idempotent"]) == {"search"}  # type: ignore[arg-type]


def test_the_canonicalizers_rules_version_is_carried() -> None:
    from novafabric.replay.equivalence.canonicalize import RULES_VERSION

    assert _fp().rules_version == RULES_VERSION


# ── Property 1: deterministic ─────────────────────────────────────────────


def test_the_signature_is_stable_across_repeated_calls() -> None:
    assert _fp().signature == _fp().signature


def test_argument_key_order_does_not_move_the_signature() -> None:
    reordered = [
        ToolCall(name="search", arguments={"k": 5, "q": "novafabric"}),
        *CALLS[1:],
    ]
    assert fingerprint_run("run-1", reordered).signature == _fp().signature


def test_the_run_id_is_not_inside_the_signature() -> None:
    """Two runs of the same behaviour must fingerprint identically, or nothing compares."""
    assert _fp("run-1").signature == _fp("run-2").signature


# ── Property 2: stable across benign non-determinism ──────────────────────


def test_an_idempotent_retry_does_not_move_the_signature() -> None:
    with_retry = [CALLS[0], CALLS[0], *CALLS[1:]]
    assert (
        fingerprint_run("run-2", with_retry, idempotent=["search"]).signature
        == fingerprint_run("run-1", CALLS, idempotent=["search"]).signature
    )


def test_reordering_declared_commutable_calls_does_not_move_the_signature() -> None:
    rules = [
        RULE_NORMALIZE_ARGUMENTS,
        RULE_COLLAPSE_IDEMPOTENT_RETRIES,
        RULE_REORDER_COMMUTABLE,
    ]
    swapped = [CALLS[1], CALLS[0], CALLS[2]]
    assert (
        fingerprint_run(
            "run-2", swapped, commutable=["search", "read"], rules=rules
        ).signature
        == fingerprint_run(
            "run-1", CALLS, commutable=["search", "read"], rules=rules
        ).signature
    )


def test_a_genuine_behaviour_change_does_move_the_signature() -> None:
    """The stability tests above are only meaningful if the signature can move at all."""
    changed = [*CALLS[:2], ToolCall(name="delete", arguments={"path": "out.txt"})]
    assert fingerprint_run("run-2", changed).signature != _fp().signature


# ── Property 3: version-sensitive ─────────────────────────────────────────


def test_the_fingerprint_version_is_inside_the_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _fp().signature
    monkeypatch.setattr(fp_mod, "FINGERPRINT_VERSION", "nf155-v999")
    assert _fp().signature != before


def test_the_canonicalization_rules_version_is_inside_the_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rules change must not be able to read as 'no shift'."""
    before = _fp().signature
    real = fp_mod.canonicalize

    def bumped(calls, **kwargs):  # type: ignore[no-untyped-def]
        result = real(calls, **kwargs)
        result.rules_version = result.rules_version + "-next"
        return result

    monkeypatch.setattr(fp_mod, "canonicalize", bumped)
    assert _fp().signature != before


# ── The basis ─────────────────────────────────────────────────────────────


def test_basis_lists_only_what_was_observed() -> None:
    assert _fp().basis == [BASIS_TRAJECTORY, BASIS_TOOL_MIX]
    assert fingerprint_run("run-1", CALLS, scores=[0.9, 0.8]).basis == [
        BASIS_TRAJECTORY,
        BASIS_TOOL_MIX,
        BASIS_SCORE_PROFILE,
    ]
    assert fingerprint_run("run-1", [], scores=[0.9]).basis == [BASIS_SCORE_PROFILE]


def test_a_run_with_nothing_observable_is_refused() -> None:
    """A signature over nothing compares equal to every other nothing."""
    with pytest.raises(FingerprintError, match="no observable behaviour"):
        fingerprint_run("run-1", [])


def test_tool_counts_are_the_canonicalized_counts() -> None:
    with_retry = [CALLS[0], CALLS[0], *CALLS[1:]]
    assert fingerprint_run("run-1", with_retry, idempotent=["search"]).tool_counts == {
        "search": 1,
        "read": 1,
        "write": 1,
    }


def test_mapping_calls_are_accepted_like_toolcalls() -> None:
    as_dicts = [{"name": c.name, "arguments": c.arguments} for c in CALLS]
    assert fingerprint_run("run-1", as_dicts).signature == _fp().signature


# ── Scores: bounded, higher-is-better, never clamped ──────────────────────


@pytest.mark.parametrize("bad", [1.5, -0.1, 5.0])
def test_an_out_of_range_score_is_refused_not_clamped(bad: float) -> None:
    with pytest.raises(FingerprintError, match=r"outside \[0, 1\]"):
        fingerprint_run("run-1", CALLS, scores=[0.5, bad])


def test_the_score_profile_keeps_n_alongside_the_mean() -> None:
    profile = fingerprint_run("run-1", CALLS, scores=[1.0, 0.0]).score_profile
    assert profile is not None
    assert (profile.n, profile.mean, profile.minimum, profile.maximum) == (
        2,
        0.5,
        0.0,
        1.0,
    )


# ── distance is a metric, not a digest comparison ─────────────────────────


def test_an_identical_run_has_distance_zero_and_is_not_shifted() -> None:
    comparison = compare_fingerprints(_fp("run-2"), _fp("run-1"), threshold=0.2)
    assert comparison.distance == 0.0
    assert comparison.shifted is False


def test_distance_grows_with_how_different_the_behaviour_is() -> None:
    """A boolean dressed as a number would return the same value for both of these."""
    baseline = _fp("base")
    one_step = fingerprint_run(
        "run-a", [*CALLS[:2], ToolCall(name="delete", arguments={"path": "out.txt"})]
    )
    nothing_shared = fingerprint_run(
        "run-b",
        [
            ToolCall(name="deploy", arguments={"env": "prod"}),
            ToolCall(name="rollback", arguments={"env": "prod"}),
            ToolCall(name="page", arguments={"who": "oncall"}),
        ],
    )
    near = compare_fingerprints(one_step, baseline, threshold=0.2)
    far = compare_fingerprints(nothing_shared, baseline, threshold=0.2)
    assert near.distance is not None and far.distance is not None
    assert 0.0 < near.distance < far.distance <= 1.0
    assert near.shifted is True and far.shifted is True


def test_every_component_distance_is_reported_alongside_the_total() -> None:
    a = fingerprint_run("run-a", CALLS, scores=[0.9])
    b = fingerprint_run("run-b", CALLS, scores=[0.4])
    comparison = compare_fingerprints(a, b, threshold=0.1)
    by_component = {c.component: c.distance for c in comparison.components}
    assert by_component[BASIS_TRAJECTORY] == 0.0
    assert by_component[BASIS_TOOL_MIX] == 0.0
    assert by_component[BASIS_SCORE_PROFILE] == pytest.approx(0.5)
    assert comparison.distance == pytest.approx(0.5 / 3)


def test_the_tool_mix_is_a_proportion_not_a_volume() -> None:
    """Doing twice as much of everything is not a change of mix."""
    doubled = fingerprint_run("run-b", [*CALLS, *CALLS])
    mix = {
        c.component: c.distance
        for c in compare_fingerprints(doubled, _fp("run-a"), threshold=0.9).components
    }
    assert mix[BASIS_TOOL_MIX] == 0.0
    assert mix[BASIS_TRAJECTORY] > 0.0  # volume shows up in the trajectory instead


def test_a_component_missing_on_one_side_does_not_contribute_as_zero() -> None:
    """Averaging an absent component in as zero would dilute a real shift to 'unchanged'."""
    scored = fingerprint_run("run-a", CALLS, scores=[0.9])
    unscored = fingerprint_run("run-b", CALLS)
    comparison = compare_fingerprints(scored, unscored, threshold=0.1)
    assert [c.component for c in comparison.components] == [
        BASIS_TRAJECTORY,
        BASIS_TOOL_MIX,
    ]
    assert comparison.basis == [BASIS_TRAJECTORY, BASIS_TOOL_MIX]


def test_no_shared_component_reports_unknown_never_unchanged() -> None:
    trajectory_only = _fp("run-a")
    scores_only = fingerprint_run("run-b", [], scores=[0.9])
    comparison = compare_fingerprints(scores_only, trajectory_only, threshold=0.1)
    assert comparison.distance is None
    assert comparison.shifted is None
    assert comparison.note is not None and "unknown" in comparison.note


def test_the_threshold_is_a_strict_boundary() -> None:
    # Scores-only, and both exactly representable, so the boundary is tested rather than a
    # floating-point artefact next to it.
    a = fingerprint_run("run-a", [], scores=[0.5])
    b = fingerprint_run("run-b", [], scores=[0.25])
    exact = compare_fingerprints(a, b, threshold=0.25)
    assert exact.distance == 0.25
    assert exact.shifted is False  # equal to the threshold is not over it
    assert compare_fingerprints(a, b, threshold=0.2).shifted is True


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_a_threshold_outside_the_bounded_range_is_refused(bad: float) -> None:
    with pytest.raises(FingerprintError, match=r"outside \[0, 1\]"):
        compare_fingerprints(_fp("a"), _fp("b"), threshold=bad)


# ── Incompatible fingerprints are refused, not scored ─────────────────────


def test_comparing_across_rule_versions_is_refused() -> None:
    baseline = _fp("base")
    other = baseline.model_copy(update={"rules_version": "999"})
    with pytest.raises(IncomparableFingerprintsError, match="rule versions differ"):
        compare_fingerprints(_fp("run-1"), other, threshold=0.1)


def test_comparing_across_fingerprint_versions_is_refused() -> None:
    baseline = _fp("base")
    other = baseline.model_copy(update={"fingerprint_version": "nf155-v0"})
    with pytest.raises(IncomparableFingerprintsError, match="fingerprint versions differ"):
        compare_fingerprints(_fp("run-1"), other, threshold=0.1)


# ── An observation, not a verdict ─────────────────────────────────────────


def test_the_comparison_carries_no_verdict_field() -> None:
    fields = set(
        compare_fingerprints(_fp("a"), _fp("b"), threshold=0.1).model_dump().keys()
    )
    assert not fields & {"regressed", "failed", "ok", "passed", "verdict"}


# ── Facet ─────────────────────────────────────────────────────────────────


def test_the_facet_round_trips_and_is_additive() -> None:
    comparison = compare_fingerprints(_fp("run-1"), _fp("base"), threshold=0.1)
    capsule: dict[str, object] = {"run_id": "run-1"}
    attached = attach_facet(capsule, comparison)

    assert capsule == {"run_id": "run-1"}  # the input is not mutated
    assert set(attached["facets"]) == {FACET_NAME}  # type: ignore[arg-type]
    read_back = facet_from_capsule(attached)
    assert read_back is not None and read_back.signature == comparison.signature


def test_attaching_nothing_leaves_the_capsule_byte_identical() -> None:
    capsule = {"run_id": "run-1"}
    assert attach_facet(capsule, None) == capsule


def test_an_invalid_facet_is_reported_not_silently_dropped() -> None:
    with pytest.raises(FingerprintError, match="invalid fingerprint facet"):
        facet_from_capsule({"facets": {FACET_NAME: {"run_id": "run-1"}}})


def test_a_capsule_without_the_facet_reads_as_none() -> None:
    assert facet_from_capsule({"run_id": "run-1"}) is None
    assert facet_from_capsule({"facets": {}}) is None
