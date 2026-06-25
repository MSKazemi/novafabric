from __future__ import annotations

import pytest

from novafabric.judge._kappa import compute_kappa, interpret_kappa


class TestComputeKappa:
    def test_perfect_agreement_all_pass(self) -> None:
        """All judges vote 'pass' → kappa = 1.0."""
        judgments = [["pass", "pass", "pass"]]
        result = compute_kappa(judgments)
        assert result == pytest.approx(1.0)

    def test_perfect_agreement_all_fail(self) -> None:
        """All judges vote 'fail' → kappa = 1.0."""
        judgments = [["fail", "fail", "fail"]]
        result = compute_kappa(judgments)
        assert result == pytest.approx(1.0)

    def test_no_agreement_half_pass_half_fail(self) -> None:
        """Two judges: one always pass, one always fail across many items → kappa near 0."""
        # 4 items: judge1 always pass, judge2 always fail
        # Observed agreement = 0, expected agreement = p_pass^2 + p_fail^2 = 0.25 + 0.25 = 0.5
        # kappa = (0 - 0.5) / (1 - 0.5) = -1.0
        judgments = [
            ["pass", "fail"],
            ["pass", "fail"],
            ["pass", "fail"],
            ["pass", "fail"],
        ]
        result = compute_kappa(judgments)
        assert result == pytest.approx(-1.0)

    def test_two_raters_exact_agreement(self) -> None:
        """Two raters agree on every item."""
        judgments = [
            ["pass", "pass"],
            ["fail", "fail"],
            ["pass", "pass"],
        ]
        result = compute_kappa(judgments)
        assert result == pytest.approx(1.0)

    def test_single_item_two_raters_disagree(self) -> None:
        """Single item, two raters disagree: pass vs fail."""
        judgments = [["pass", "fail"]]
        # Observed P_o = 0
        # p_pass = 0.5, p_fail = 0.5 → P_e = 0.5
        # kappa = (0 - 0.5) / (1 - 0.5) = -1.0
        result = compute_kappa(judgments)
        assert result == pytest.approx(-1.0)

    def test_single_judge_per_item_returns_one(self) -> None:
        """list[list[str]] with one judge per item → kappa=1.0 (trivially)."""
        # Only 1 rater per item: not meaningful, returns perfect agreement
        judgments = [["pass"], ["fail"], ["pass"]]
        result = compute_kappa(judgments)
        assert result == pytest.approx(1.0)

    def test_empty_judgments_returns_one(self) -> None:
        """Empty list → 1.0 (no disagreement possible)."""
        assert compute_kappa([]) == pytest.approx(1.0)

    def test_kappa_with_three_classes(self) -> None:
        """Uncertain is a valid third class; kappa handles it gracefully."""
        # 3 items, 2 raters
        judgments = [
            ["pass", "pass"],
            ["fail", "uncertain"],
            ["uncertain", "uncertain"],
        ]
        result = compute_kappa(judgments)
        # kappa must be in [-1, 1]
        assert -1.0 <= result <= 1.0

    def test_kappa_single_judge_list_input(self) -> None:
        """Single judge across multiple items (one-element lists) → 1.0."""
        judgments = [["pass"], ["pass"], ["fail"]]
        result = compute_kappa(judgments)
        assert result == pytest.approx(1.0)

    def test_kappa_moderate_agreement(self) -> None:
        """Two judges mostly agreeing → kappa between 0 and 1."""
        judgments = [
            ["pass", "pass"],
            ["pass", "pass"],
            ["fail", "fail"],
            ["pass", "fail"],  # one disagreement
        ]
        result = compute_kappa(judgments)
        assert 0.0 < result < 1.0


class TestInterpretKappa:
    def test_almost_perfect(self) -> None:
        assert interpret_kappa(0.9) == "almost perfect agreement"

    def test_substantial(self) -> None:
        assert interpret_kappa(0.7) == "substantial agreement"

    def test_moderate(self) -> None:
        assert interpret_kappa(0.5) == "moderate agreement"

    def test_fair(self) -> None:
        assert interpret_kappa(0.3) == "fair agreement"

    def test_slight(self) -> None:
        assert interpret_kappa(0.1) == "slight agreement"

    def test_no_agreement(self) -> None:
        assert interpret_kappa(0.0) == "no agreement"

    def test_negative_kappa(self) -> None:
        assert interpret_kappa(-0.5) == "no agreement"
