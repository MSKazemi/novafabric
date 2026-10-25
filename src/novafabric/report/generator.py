from __future__ import annotations

import json
import re
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


def _md_flat(value: object) -> str:
    """*value* on one line with table-breaking characters neutralised (control chars, ``|``)."""
    text = "".join(" " if (ord(c) < 32 or ord(c) == 127) else c for c in str(value))
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _md_code_cell(value: object) -> str:
    """*value* as a Markdown code span that is safe inside a table cell.

    Asset names are arbitrary strings: raw, a ``|`` or a newline broke the row and
    ``[x](javascript:...)``, raw HTML or ``#`` injected links and headings into a report
    pasted into a PR, a wiki or a compliance pack, while the HTML report escapes them
    (review misc-1-11). A code span renders its content literally; the fence is longer than
    any backtick run inside, pipes are escaped, and line breaks and control characters
    become spaces.
    """
    text = "".join(" " if (ord(c) < 32 or ord(c) == 127) else c for c in str(value))
    text = text.replace("|", "\\|")
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


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
                f"| {_md_code_cell(a['name'])} | {_md_code_cell(a['version'])} "
                f"| {_md_flat(a['status'])} | {created} |"
            )
        lines.append("")
    return "\n".join(lines)
