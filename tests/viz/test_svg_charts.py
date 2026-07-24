"""Tests for the shared pure-stdlib SVG chart engine (``novafabric.viz.svg``).

The single-series functions carry the exact ADR-0131 trend-chart behavior
(gap-aware line, categorical bars); the multi-series functions add stacked
bars and multiple gap-aware lines. All charts must be self-contained inline
SVG with escaped text and no external references.
"""

from __future__ import annotations

import re
from typing import Any

from novafabric.viz.svg import (
    CHART_CSS,
    DEFAULT_PALETTE,
    svg_bar_chart,
    svg_line_chart,
    svg_multi_line_chart,
    svg_stacked_bar_chart,
)


def _points(*values: float | None) -> list[dict[str, Any]]:
    return [{"bucket": f"b{i}", "value": v} for i, v in enumerate(values)]


def _multi(keys: list[str], *rows: dict[str, float | None]) -> list[dict[str, Any]]:
    return [
        {"bucket": f"b{i}", "values": {k: row.get(k) for k in keys}}
        for i, row in enumerate(rows)
    ]


# ── geometry basics ──────────────────────────────────────────────────────────


def test_line_chart_is_complete_svg_with_axes_and_marks() -> None:
    svg = svg_line_chart(_points(1.0, 2.0, 3.0))
    assert svg is not None
    assert svg.startswith('<svg viewBox="0 0 720 260"')
    assert svg.endswith("</svg>")
    assert len(re.findall(r'<line class="axis"', svg)) == 2  # y axis + x axis
    assert len(re.findall(r"<circle ", svg)) == 3  # one mark per point
    assert len(re.findall(r"<path ", svg)) == 1  # one contiguous run
    # Y-domain is zero-based: min/max tick labels rendered.
    assert ">0</text>" in svg
    assert ">3</text>" in svg


def test_line_chart_x_positions_are_slot_centered_and_increasing() -> None:
    svg = svg_line_chart(_points(1.0, 1.0, 1.0, 1.0))
    assert svg is not None
    xs = [float(m) for m in re.findall(r'<circle [^>]*cx="([0-9.]+)"', svg)]
    assert xs == sorted(xs)
    # 4 slots over plot width 640 starting at x=64: first center = 64 + 80.
    assert xs[0] == 144.0


def test_bar_chart_draws_one_rect_per_non_null_bucket() -> None:
    svg = svg_bar_chart(_points(2.0, None, 4.0))
    assert svg is not None
    rects = re.findall(r"<rect [^>]*/>", svg)
    assert len(rects) == 2  # the null bucket draws no bar
    heights = [float(m) for m in re.findall(r'<rect [^>]*height="([0-9.]+)"', svg)]
    # Zero-based domain to max=4 over a 200px plot: 2.0 → 100px, 4.0 → 200px.
    assert heights == [100.0, 200.0]


# ── gap handling ─────────────────────────────────────────────────────────────


def test_line_chart_gap_breaks_the_line_never_draws_zero() -> None:
    svg = svg_line_chart(_points(1.0, 2.0, None, 3.0, 4.0))
    assert svg is not None
    # Two contiguous runs of >= 2 points → two paths; the gap draws nothing.
    assert len(re.findall(r"<path ", svg)) == 2
    assert len(re.findall(r"<circle ", svg)) == 4


def test_line_chart_isolated_point_renders_mark_only() -> None:
    svg = svg_line_chart(_points(None, 5.0, None))
    assert svg is not None
    assert "<path " not in svg
    assert len(re.findall(r"<circle ", svg)) == 1


def test_multi_line_chart_gap_splits_only_that_keys_line() -> None:
    series = _multi(
        ["a", "b"],
        {"a": 1.0, "b": 5.0},
        {"a": None, "b": 6.0},
        {"a": 3.0, "b": 7.0},
        {"a": 4.0, "b": 8.0},
    )
    svg = svg_multi_line_chart(series, ["a", "b"])
    assert svg is not None
    # Key "a" splits at the gap but its runs are 1 point each side... no:
    # runs are [b0] and [b2,b3] → one polyline; key "b" is unbroken → one.
    assert len(re.findall(r"<polyline ", svg)) == 2
    # 3 non-null "a" points + 4 "b" points → 7 marks.
    assert len(re.findall(r"<circle ", svg)) == 7


# ── XML escaping ─────────────────────────────────────────────────────────────


def test_bucket_labels_are_xml_escaped() -> None:
    series = [{"bucket": "<b>evil</b> & co", "value": 1.0}]
    svg = svg_bar_chart(series)
    assert svg is not None
    assert "<b>evil</b>" not in svg
    assert "&lt;b&gt;evil&lt;/b&gt; &amp; co" in svg


def test_long_bucket_labels_are_truncated_with_ellipsis() -> None:
    series = [{"bucket": "a-very-long-asset-identifier@1.0.0", "value": 1.0}]
    svg = svg_line_chart(series)
    assert svg is not None
    assert "a-very-long-asset…" in svg
    assert "identifier@1.0.0" not in svg


# ── empty / insufficient series → None ───────────────────────────────────────


def test_empty_and_all_null_series_return_none() -> None:
    assert svg_line_chart([]) is None
    assert svg_bar_chart([]) is None
    assert svg_line_chart(_points(None, None)) is None
    assert svg_bar_chart(_points(None)) is None
    assert svg_stacked_bar_chart([], ["a"]) is None
    assert svg_multi_line_chart([], ["a"]) is None
    all_null = _multi(["a"], {"a": None}, {"a": None})
    assert svg_stacked_bar_chart(all_null, ["a"]) is None
    assert svg_multi_line_chart(all_null, ["a"]) is None


def test_no_keys_returns_none() -> None:
    series = _multi(["a"], {"a": 1.0})
    assert svg_stacked_bar_chart(series, []) is None
    assert svg_multi_line_chart(series, []) is None


# ── stacked bars ─────────────────────────────────────────────────────────────


def test_stacked_bar_segment_heights_sum_to_bar_total() -> None:
    # Max stack total 4.0 over the 200px plot height → 50px per unit.
    series = _multi(
        ["a", "b", "c"],
        {"a": 1.0, "b": 1.0, "c": 2.0},
        {"a": 2.0, "b": None, "c": 0.0},
    )
    svg = svg_stacked_bar_chart(series, ["a", "b", "c"])
    assert svg is not None
    heights = [float(m) for m in re.findall(r'<rect [^>]*height="([0-9.]+)"', svg)]
    # Bar 1: three segments 50+50+100 = 200 (full height, it is the max).
    # Bar 2: only "a" draws (None and 0.0 segments are skipped) → 100.
    assert heights == [50.0, 50.0, 100.0, 100.0]
    assert sum(heights[:3]) == 200.0


def test_stacked_bar_segments_stack_upward_and_use_palette() -> None:
    series = _multi(["a", "b"], {"a": 1.0, "b": 1.0})
    svg = svg_stacked_bar_chart(series, ["a", "b"])
    assert svg is not None
    rects = re.findall(r'<rect [^>]*y="([0-9.]+)"[^>]*fill="([^"]+)"', svg)
    assert [fill for _, fill in rects] == [DEFAULT_PALETTE[0], DEFAULT_PALETTE[1]]
    # Key order is bottom-up: the first key's segment sits below the second's.
    assert float(rects[0][0]) > float(rects[1][0])


def test_stacked_bar_cycles_palette_and_accepts_custom_colors() -> None:
    keys = ["k1", "k2", "k3", "k4"]
    series = _multi(keys, dict.fromkeys(keys, 1.0))
    svg = svg_stacked_bar_chart(series, keys)
    assert svg is not None
    fills = re.findall(r'fill="([^"]+)"', svg)
    assert fills[3] == DEFAULT_PALETTE[0]  # 4th key cycles back
    custom = svg_stacked_bar_chart(series, keys, colors=["#111111"])
    assert custom is not None
    assert set(re.findall(r'fill="([^"]+)"', custom)) == {"#111111"}


# ── multi-line ───────────────────────────────────────────────────────────────


def test_multi_line_renders_one_polyline_per_key() -> None:
    series = _multi(
        ["a", "b", "c"],
        {"a": 1.0, "b": 2.0, "c": 3.0},
        {"a": 2.0, "b": 3.0, "c": 4.0},
    )
    svg = svg_multi_line_chart(series, ["a", "b", "c"])
    assert svg is not None
    strokes = re.findall(r'<polyline [^>]*stroke="([^"]+)"', svg)
    assert strokes == list(DEFAULT_PALETTE)  # one gap-free polyline per key
    assert len(re.findall(r"<circle ", svg)) == 6


def test_multi_line_shares_one_y_domain_across_keys() -> None:
    series = _multi(["lo", "hi"], {"lo": 1.0, "hi": 100.0}, {"lo": 2.0, "hi": 200.0})
    svg = svg_multi_line_chart(series, ["lo", "hi"])
    assert svg is not None
    assert ">200</text>" in svg  # shared max tick
    assert ">0</text>" in svg  # zero-based shared min tick


# ── CHART_CSS ────────────────────────────────────────────────────────────────


def test_chart_css_is_non_empty_and_styles_chart_classes() -> None:
    assert CHART_CSS.strip()
    for selector in ("svg", ".mark", ".stroke", ".axis", ".tick"):
        assert selector in CHART_CSS
