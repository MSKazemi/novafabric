from __future__ import annotations

import json

from novafabric.diff._report import DiffReport


def format_text(report: DiffReport) -> str:
    lines: list[str] = []
    lines.append(f"Diff: {report.run_a_id} → {report.run_b_id}")
    lines.append(
        f"  changed={report.changed_count}"
        f"  added={report.added_count}"
        f"  removed={report.removed_count}"
    )
    lines.append("")

    if report.env_changes:
        lines.append("Environment:")
        for ch in report.env_changes:
            lines.append(f"  ~ {ch.get('field')}: {ch.get('before')!r} → {ch.get('after')!r}")

    changed_models = [p for p in report.model_call_pairs if p.get("changed")]
    added_models = [p for p in report.model_call_pairs if p.get("added")]
    removed_models = [p for p in report.model_call_pairs if p.get("removed")]
    if changed_models or added_models or removed_models:
        lines.append("Model calls:")
        for p in changed_models:
            lines.append(f"  ~ call at span {p.get('span_id', '?')}")
        for p in added_models:
            lines.append(f"  + call at span {p.get('span_id', '?')} (added)")
        for p in removed_models:
            lines.append(f"  - call at span {p.get('span_id', '?')} (removed)")

    changed_tools = [p for p in report.tool_call_pairs if p.get("changed")]
    added_tools = [p for p in report.tool_call_pairs if p.get("added")]
    removed_tools = [p for p in report.tool_call_pairs if p.get("removed")]
    if changed_tools or added_tools or removed_tools:
        lines.append("Tool calls:")
        for p in changed_tools:
            lines.append(f"  ~ {p.get('tool_name', '?')}")
        for p in added_tools:
            lines.append(f"  + {p.get('tool_name', '?')} (added)")
        for p in removed_tools:
            lines.append(f"  - {p.get('tool_name', '?')} (removed)")

    if report.output_changes:
        lines.append("Outputs:")
        for ch in report.output_changes:
            lines.append(f"  ~ {ch.get('path')}")

    if not (
        report.env_changes or changed_models or added_models or removed_models
        or changed_tools or added_tools or removed_tools or report.output_changes
    ):
        lines.append("No differences found.")

    return "\n".join(lines)


def format_json(report: DiffReport) -> str:
    return json.dumps(report.as_dict(), indent=2)


def format_github_annotations(report: DiffReport) -> str:
    lines: list[str] = []
    level = "error" if report.changed_count > 0 else "notice"

    if report.env_changes:
        for ch in report.env_changes:
            lines.append(
                f"::{level} title=NovaFabric Diff::Environment field changed: "
                f"{ch.get('field')} {ch.get('before')!r} → {ch.get('after')!r}"
            )

    for p in report.model_call_pairs:
        span = p.get("span_id", "?")
        if p.get("changed"):
            lines.append(f"::{level} title=NovaFabric Diff::Model call changed at span {span}")
        elif p.get("added"):
            lines.append(f"::{level} title=NovaFabric Diff::Model call added")
        elif p.get("removed"):
            lines.append(f"::{level} title=NovaFabric Diff::Model call removed")

    for p in report.tool_call_pairs:
        name = p.get("tool_name", "?")
        if p.get("changed"):
            lines.append(f"::{level} title=NovaFabric Diff::Tool call changed: {name}")
        elif p.get("added"):
            lines.append(f"::{level} title=NovaFabric Diff::Tool call added: {name}")
        elif p.get("removed"):
            lines.append(f"::{level} title=NovaFabric Diff::Tool call removed: {name}")

    for ch in report.output_changes:
        lines.append(f"::{level} title=NovaFabric Diff::Output changed: {ch.get('path')}")

    if not lines:
        lines.append("::notice title=NovaFabric Diff::No differences found.")

    return "\n".join(lines)
