from __future__ import annotations

from collections import Counter


def compute_kappa(judgments: list[list[str]]) -> float:
    """Compute Cohen's kappa for inter-judge agreement.

    Args:
        judgments: list of judge verdicts per item.
                   judgments[i] = [judge1_verdict, judge2_verdict, ...] for item i

    Returns:
        Cohen's kappa (-1.0 to 1.0). 1.0 = perfect agreement, 0.0 = chance agreement.
        Returns 1.0 for empty input or single item with all judges agreeing.
    """
    if not judgments:
        return 1.0

    # Filter to items that have at least 2 raters
    valid = [item for item in judgments if len(item) >= 2]
    if not valid:
        return 1.0

    n_items = len(valid)
    n_raters = len(valid[0])

    # Check that all items have the same number of raters
    if any(len(item) != n_raters for item in valid):
        # Use the minimum length to compare pairwise
        n_raters = min(len(item) for item in valid)
        valid = [item[:n_raters] for item in valid]

    # Collect all categories
    all_labels: list[str] = []
    for item in valid:
        all_labels.extend(item)
    categories = sorted(set(all_labels))

    if len(categories) <= 1:
        # All labels identical — perfect agreement
        return 1.0

    # Observed agreement: fraction of pairs within each item that agree
    # Using the standard multi-rater Fleiss kappa formula for n raters per item.
    # For two raters this equals Cohen's kappa.
    #
    # P_o = (1/n) * sum_i [ (1 / (k*(k-1))) * sum_j (n_ij * (n_ij - 1)) ]
    # where n is number of items, k is number of raters, n_ij is count of category j for item i

    p_o_sum = 0.0
    category_totals: Counter[str] = Counter()

    for item in valid:
        counts: Counter[str] = Counter(item)
        for cat in categories:
            c = counts[cat]
            p_o_sum += c * (c - 1)
            category_totals[cat] += c

    k = n_raters
    p_o = p_o_sum / (n_items * k * (k - 1)) if n_items * k * (k - 1) > 0 else 1.0

    # Expected agreement: sum_j p_j^2 where p_j is marginal probability of category j
    total_ratings = n_items * k
    p_e = sum(
        (category_totals[cat] / total_ratings) ** 2 for cat in categories
    )

    if p_e >= 1.0:
        # All raters assigned the same category — perfect agreement
        return 1.0

    kappa = (p_o - p_e) / (1.0 - p_e)
    return float(kappa)


def interpret_kappa(kappa: float) -> str:
    """Return a human-readable interpretation."""
    if kappa > 0.8:
        return "almost perfect agreement"
    elif kappa > 0.6:
        return "substantial agreement"
    elif kappa > 0.4:
        return "moderate agreement"
    elif kappa > 0.2:
        return "fair agreement"
    elif kappa > 0.0:
        return "slight agreement"
    else:
        return "no agreement"
