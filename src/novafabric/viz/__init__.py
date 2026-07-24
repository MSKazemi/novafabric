"""Shared pure-stdlib visualization primitives (inline SVG charts).

Extracted from ``novafabric.trend.html`` (ADR-0131) so any HTML-emitting
surface can pre-render charts without a charting library, JavaScript, or
external requests — the ADR-0038 "inline SVG, no charting library"
precedent. Status: **experimental**.
"""

from novafabric.viz.svg import (
    CHART_CSS,
    DEFAULT_PALETTE,
    svg_bar_chart,
    svg_line_chart,
    svg_multi_line_chart,
    svg_stacked_bar_chart,
)

__all__ = [
    "CHART_CSS",
    "DEFAULT_PALETTE",
    "svg_bar_chart",
    "svg_line_chart",
    "svg_multi_line_chart",
    "svg_stacked_bar_chart",
]
