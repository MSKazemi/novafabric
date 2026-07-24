"""Pure-stdlib inline-SVG chart engine (shared; extracted from ``trend.html``).

Pre-renders small, self-contained ``<svg>`` charts with the Python standard
library only — no charting dependency, no JavaScript, no external requests —
mirroring the ADR-0038 "inline SVG, no charting library" precedent. Every
chart function returns a complete ``<svg …>…</svg>`` string, or ``None``
when the series carries no plottable data (the host renders its own empty
state).

Chart functions
---------------
- :func:`svg_line_chart` — single line over ordered buckets; ``value: None``
  points break the line into visible gaps (never drawn as zero).
- :func:`svg_bar_chart` — one bar per bucket (categorical groupings);
  ``None`` buckets draw no bar.
- :func:`svg_stacked_bar_chart` — one bar per series entry, one stacked
  segment per key (non-negative values; ``None``/missing segments are
  skipped).
- :func:`svg_multi_line_chart` — one gap-aware line per key over a shared
  x axis.

Single-series charts (``svg_line_chart`` / ``svg_bar_chart``) take the
TrendReport series shape: ``[{"bucket": str, "value": float | None}, …]``.
Multi-series charts take ``[{"bucket": str, "values": {key: float | None}},
…]`` plus the ordered ``keys`` to plot.

Multi-series colors default to the palette ``#3987e5`` (blue), ``#d94f4f``
(red), ``#c98500`` (amber), cycled when there are more keys than colors.

Hosts embedding these charts should inline :data:`CHART_CSS` in their
``<style>`` block — it styles the axis, tick-label, and single-series
mark/stroke classes. Status: **experimental**.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from xml.sax.saxutils import escape

_WIDTH = 720
_HEIGHT = 260
_ML, _MR, _MT, _MB = 64, 16, 16, 44  # margins: left, right, top, bottom
_MAX_X_LABELS = 10
_MAX_LABEL_CHARS = 18

#: Default multi-series palette (cycled when keys outnumber colors).
DEFAULT_PALETTE: tuple[str, ...] = ("#3987e5", "#d94f4f", "#c98500")

#: Chart-related CSS for host pages to inline in their ``<style>`` block.
CHART_CSS = """\
svg { max-width: 100%; height: auto; }
.mark { fill: rgba(30,110,190,.85); }
.stroke { stroke: rgba(30,110,190,.85); stroke-width: 2; fill: none; }
.axis { stroke: rgba(128,128,128,.7); stroke-width: 1; }
.tick { font-size: 10px; fill: currentColor; }"""


def _fmt(value: float) -> str:
    return f"{value:.6g}"


def _scale(values: list[float]) -> tuple[float, float]:
    """Y-axis domain: zero-based unless the data dips below zero."""
    low = min(0.0, min(values))
    high = max(values)
    if high == low:
        high = low + 1.0
    return low, high


def svg_line_chart(series: list[dict[str, Any]]) -> str | None:
    """Pre-rendered line chart; gap points break the line (never zero)."""
    values = [p["value"] for p in series if p["value"] is not None]
    if not values:
        return None
    low, high = _scale([float(v) for v in values])
    plot_w = _WIDTH - _ML - _MR
    plot_h = _HEIGHT - _MT - _MB
    n = len(series)

    def x_of(i: int) -> float:
        return _ML + (i + 0.5) * plot_w / n

    def y_of(v: float) -> float:
        return _MT + plot_h * (1.0 - (v - low) / (high - low))

    parts: list[str] = [_axes(low, high)]
    # Contiguous non-null runs become one <path> each; a gap is a visible break.
    segment: list[tuple[float, float]] = []

    def flush() -> None:
        if len(segment) >= 2:
            d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in segment)
            parts.append(f'<path class="stroke" d="{d}"/>')
        segment.clear()

    for i, point in enumerate(series):
        if point["value"] is None:
            flush()
            continue
        xy = (x_of(i), y_of(float(point["value"])))
        segment.append(xy)
        parts.append(f'<circle class="mark" cx="{xy[0]:.1f}" cy="{xy[1]:.1f}" r="3"/>')
    flush()
    parts.append(_x_labels(series, x_of))
    return _svg(parts)


def svg_bar_chart(series: list[dict[str, Any]]) -> str | None:
    """Pre-rendered bar chart for categorical groupings."""
    values = [float(p["value"]) for p in series if p["value"] is not None]
    if not values:
        return None
    low, high = _scale(values)
    plot_w = _WIDTH - _ML - _MR
    plot_h = _HEIGHT - _MT - _MB
    n = len(series)
    slot = plot_w / n
    bar_w = slot * 0.6

    def x_of(i: int) -> float:
        return _ML + (i + 0.5) * slot

    def y_of(v: float) -> float:
        return _MT + plot_h * (1.0 - (v - low) / (high - low))

    parts: list[str] = [_axes(low, high)]
    zero_y = y_of(max(low, 0.0))
    for i, point in enumerate(series):
        if point["value"] is None:
            continue
        top = y_of(float(point["value"]))
        y0, y1 = min(top, zero_y), max(top, zero_y)
        parts.append(
            f'<rect class="mark" x="{x_of(i) - bar_w / 2:.1f}" y="{y0:.1f}" '
            f'width="{bar_w:.1f}" height="{max(y1 - y0, 1.0):.1f}"/>'
        )
    parts.append(_x_labels(series, x_of))
    return _svg(parts)


def svg_stacked_bar_chart(
    series: list[dict[str, Any]],
    keys: Sequence[str],
    colors: Sequence[str] | None = None,
) -> str | None:
    """Stacked bar chart: one bar per series entry, one segment per key.

    Each entry is ``{"bucket": str, "values": {key: float | None}}``.
    Segment values must be non-negative; ``None``/missing segments are
    skipped. Returns ``None`` when there is nothing to plot.
    """
    if not series or not keys:
        return None
    palette = list(colors) if colors else list(DEFAULT_PALETTE)
    totals: list[float] = []
    any_value = False
    for point in series:
        vals = point.get("values") or {}
        total = 0.0
        for key in keys:
            v = vals.get(key)
            if v is not None:
                any_value = True
                total += float(v)
        totals.append(total)
    if not any_value:
        return None
    low, high = _scale(totals)
    plot_w = _WIDTH - _ML - _MR
    plot_h = _HEIGHT - _MT - _MB
    n = len(series)
    slot = plot_w / n
    bar_w = slot * 0.6

    def x_of(i: int) -> float:
        return _ML + (i + 0.5) * slot

    def y_of(v: float) -> float:
        return _MT + plot_h * (1.0 - (v - low) / (high - low))

    parts: list[str] = [_axes(low, high)]
    for i, point in enumerate(series):
        vals = point.get("values") or {}
        cum = 0.0
        for k, key in enumerate(keys):
            v = vals.get(key)
            if v is None or float(v) <= 0.0:
                continue
            top = y_of(cum + float(v))
            bottom = y_of(cum)
            parts.append(
                f'<rect x="{x_of(i) - bar_w / 2:.1f}" y="{top:.1f}" '
                f'width="{bar_w:.1f}" height="{bottom - top:.1f}" '
                f'fill="{escape(palette[k % len(palette)])}"/>'
            )
            cum += float(v)
    parts.append(_x_labels(series, x_of))
    return _svg(parts)


def svg_multi_line_chart(
    series: list[dict[str, Any]],
    keys: Sequence[str],
    colors: Sequence[str] | None = None,
) -> str | None:
    """Multiple gap-aware lines over a shared x axis, one per key.

    Each entry is ``{"bucket": str, "values": {key: float | None}}``.
    ``None``/missing points break that key's line into visible gaps (never
    drawn as zero). Returns ``None`` when there is nothing to plot.
    """
    if not series or not keys:
        return None
    palette = list(colors) if colors else list(DEFAULT_PALETTE)
    values: list[float] = []
    for point in series:
        vals = point.get("values") or {}
        values.extend(float(vals[key]) for key in keys if vals.get(key) is not None)
    if not values:
        return None
    low, high = _scale(values)
    plot_w = _WIDTH - _ML - _MR
    plot_h = _HEIGHT - _MT - _MB
    n = len(series)

    def x_of(i: int) -> float:
        return _ML + (i + 0.5) * plot_w / n

    def y_of(v: float) -> float:
        return _MT + plot_h * (1.0 - (v - low) / (high - low))

    parts: list[str] = [_axes(low, high)]
    for k, key in enumerate(keys):
        color = escape(palette[k % len(palette)])
        # Contiguous non-null runs become one <polyline> each; gaps break it.
        segment: list[tuple[float, float]] = []

        def flush(segment: list[tuple[float, float]] = segment, color: str = color) -> None:
            if len(segment) >= 2:
                pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in segment)
                parts.append(
                    f'<polyline fill="none" stroke="{color}" '
                    f'stroke-width="2" points="{pts}"/>'
                )
            segment.clear()

        for i, point in enumerate(series):
            vals = point.get("values") or {}
            v = vals.get(key)
            if v is None:
                flush()
                continue
            xy = (x_of(i), y_of(float(v)))
            segment.append(xy)
            parts.append(
                f'<circle fill="{color}" cx="{xy[0]:.1f}" cy="{xy[1]:.1f}" r="3"/>'
            )
        flush()
    parts.append(_x_labels(series, x_of))
    return _svg(parts)


def _axes(low: float, high: float) -> str:
    plot_bottom = _HEIGHT - _MB
    return (
        f'<line class="axis" x1="{_ML}" y1="{_MT}" x2="{_ML}" y2="{plot_bottom}"/>'
        f'<line class="axis" x1="{_ML}" y1="{plot_bottom}" '
        f'x2="{_WIDTH - _MR}" y2="{plot_bottom}"/>'
        f'<text class="tick" x="{_ML - 6}" y="{_MT + 4}" '
        f'text-anchor="end">{escape(_fmt(high))}</text>'
        f'<text class="tick" x="{_ML - 6}" y="{plot_bottom + 4}" '
        f'text-anchor="end">{escape(_fmt(low))}</text>'
    )


def _x_labels(series: list[dict[str, Any]], x_of: Callable[[int], float]) -> str:
    step = max(1, -(-len(series) // _MAX_X_LABELS))  # ceil division
    labels: list[str] = []
    for i in range(0, len(series), step):
        raw = str(series[i]["bucket"])
        if len(raw) > _MAX_LABEL_CHARS:
            raw = raw[: _MAX_LABEL_CHARS - 1] + "…"
        labels.append(
            f'<text class="tick" x="{x_of(i):.1f}" y="{_HEIGHT - _MB + 16}" '
            f'text-anchor="middle">{escape(raw)}</text>'
        )
    return "".join(labels)


def _svg(parts: list[str]) -> str:
    body = "".join(parts)
    return (
        f'<svg viewBox="0 0 {_WIDTH} {_HEIGHT}" width="{_WIDTH}" '
        f'height="{_HEIGHT}" role="img" '
        f'aria-label="trend chart">{body}</svg>'
    )
