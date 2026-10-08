from __future__ import annotations

import json

from novafabric.diff._report import DiffReport

#: Record file each ``skipped_malformed_lines`` key counts lines of.
RECORD_FILES = {"model_calls": "model-calls.jsonl", "tool_calls": "tool-calls.jsonl"}

#: Verdict line when nothing differs but record lines were skipped: "no
#: differences" is then not established, only "none among what parsed".
NO_DIFF_INCOMPLETE = (
    "No differences found in the records that parsed; the comparison is incomplete."
)


def malformed_line_messages(report: DiffReport) -> list[str]:
    """One sentence per record file with skipped malformed lines, A side first."""
    run_ids = {"a": report.run_a_id, "b": report.run_b_id}
    messages: list[str] = []
    for side in ("a", "b"):
        for key, count in report.skipped_malformed_lines.get(side, {}).items():
            if count:
                messages.append(
                    f"skipped {count} malformed line(s) in {RECORD_FILES.get(key, key)} "
                    f"of run {side.upper()} ({run_ids[side]}): not UTF-8, not JSON, "
                    "or not a JSON object"
                )
    return messages



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

    # DiffReport.has_changes is the one definition of "any difference" — the
    # --assert-no-regressions gate reads it too, so the two cannot disagree.
    malformed = malformed_line_messages(report)
    if malformed:
        lines.append("Skipped (not compared):")
        for message in malformed:
            lines.append(f"  ! {message}")

    if not report.has_changes:
        lines.append("No differences found." if report.is_complete else NO_DIFF_INCOMPLETE)

    return "\n".join(lines)


def format_json(report: DiffReport) -> str:
    return json.dumps(report.as_dict(), indent=2)


def _annotation_data(message: str) -> str:
    """Escape a workflow-command message the way ``@actions/core`` does.

    Output paths, tool names and span ids come from the workload. A newline in one
    (a legal file-name character) would end the annotation and start a second,
    attacker-chosen workflow command on the next line.
    """
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def format_github_annotations(report: DiffReport) -> str:
    # Severity follows DiffReport.has_changes, the property the gate uses. It was
    # derived from changed_count alone, so a diff whose only differences were
    # added or removed calls was annotated as a mere ``notice``.
    level = "error" if report.has_changes else "notice"
    messages: list[str] = []

    for ch in report.env_changes:
        messages.append(
            f"Environment field changed: "
            f"{ch.get('field')} {ch.get('before')!r} → {ch.get('after')!r}"
        )

    for p in report.model_call_pairs:
        span = p.get("span_id", "?")
        if p.get("changed"):
            messages.append(f"Model call changed at span {span}")
        elif p.get("added"):
            messages.append("Model call added")
        elif p.get("removed"):
            messages.append("Model call removed")

    for p in report.tool_call_pairs:
        name = p.get("tool_name", "?")
        if p.get("changed"):
            messages.append(f"Tool call changed: {name}")
        elif p.get("added"):
            messages.append(f"Tool call added: {name}")
        elif p.get("removed"):
            messages.append(f"Tool call removed: {name}")

    for ch in report.output_changes:
        messages.append(f"Output changed: {ch.get('path')}")

    lines = [
        f"::{level} title=NovaFabric Diff::{_annotation_data(m)}" for m in messages
    ]
    # A skipped record is not a difference, so it does not raise the level; it is
    # a warning that the verdict above covers only the records that parsed.
    lines.extend(
        f"::warning title=NovaFabric Diff::{_annotation_data(m[:1].upper() + m[1:])}"
        for m in malformed_line_messages(report)
    )

    if not report.has_changes:
        verdict = "No differences found." if report.is_complete else NO_DIFF_INCOMPLETE
        lines.append(f"::notice title=NovaFabric Diff::{verdict}")

    return "\n".join(lines)
