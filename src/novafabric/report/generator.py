from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from novafabric.registry.service import list_assets

_PDF_HINT = (
    "PDF report output requires WeasyPrint — install the optional extra: "
    "pip install 'novafabric[compliance]'"
)

_INVENTORY_COLS = ["name", "version", "asset_type", "status", "created_at"]


def _render_inventory_html(assets: list[dict]) -> str:  # type: ignore[type-arg]
    """Asset inventory as a self-contained HTML page with a by-type bar chart."""
    from novafabric.viz.report_html import render_report_html
    from novafabric.viz.svg import svg_bar_chart

    rows = [
        {c: (a.get(c) or "")[:19] if c == "created_at" else a.get(c) for c in _INVENTORY_COLS}
        for a in assets
    ]
    counts: dict[str, int] = {}
    for a in assets:
        counts[a["asset_type"]] = counts.get(a["asset_type"], 0) + 1
    chart = svg_bar_chart(
        [{"bucket": t, "value": n} for t, n in sorted(counts.items())]
    )
    return render_report_html(
        title="NovaFabric Asset Inventory",
        columns=_INVENTORY_COLS,
        rows=rows,
        chart_svg=chart,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        generator="nova report",
    )


def generate_report_pdf(db_path: Path | None = None) -> bytes:
    """Asset inventory PDF (HTML through the optional WeasyPrint extra).

    Raises ``RuntimeError`` with an install hint when WeasyPrint is absent —
    the same degradation pattern as the compliance renderer (ADR-0201).
    """
    try:
        from weasyprint import HTML  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(_PDF_HINT) from exc
    html = generate_report("html", db_path=db_path)
    pdf: bytes = HTML(string=html).write_pdf()
    return pdf


def generate_report(format_: str = "markdown", db_path: Path | None = None) -> str:
    assets = list_assets(asset_type=None, status=None, db_path=db_path)

    if format_ == "json":
        return json.dumps(
            [
                {k: v for k, v in a.items() if k != "spec_json"}
                for a in assets
            ],
            indent=2,
        )

    if format_ == "html":
        return _render_inventory_html(assets)

    by_type: dict[str, list[dict]] = {}  # type: ignore[type-arg]
    for a in assets:
        by_type.setdefault(a["asset_type"], []).append(a)

    lines = ["# NovaFabric Asset Inventory\n"]
    for asset_type, group in sorted(by_type.items()):
        lines.append(f"## {asset_type.capitalize()}s\n")
        lines.append("| Name | Version | Status | Created At |")
        lines.append("| ---- | ------- | ------ | ---------- |")
        for a in group:
            created = (a["created_at"] or "")[:19]
            lines.append(
                f"| {a['name']} | {a['version']} | {a['status']} | {created} |"
            )
        lines.append("")
    return "\n".join(lines)
