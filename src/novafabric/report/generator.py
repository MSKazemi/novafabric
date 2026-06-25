from __future__ import annotations

import json
from pathlib import Path

from novafabric.registry.service import list_assets


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
