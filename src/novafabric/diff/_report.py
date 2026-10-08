from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DiffReport:
    run_a_id: str
    run_b_id: str
    env_changes: list[dict[str, Any]] = field(default_factory=list)
    model_call_pairs: list[dict[str, Any]] = field(default_factory=list)
    tool_call_pairs: list[dict[str, Any]] = field(default_factory=list)
    output_changes: list[dict[str, Any]] = field(default_factory=list)
    #: Non-blank record lines the engine could not read (not UTF-8, not JSON, or
    #: not a JSON object), per side (``a``/``b``) and per record file. They take
    #: no part in the comparison, so a non-zero count means it is incomplete.
    skipped_malformed_lines: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            side: {"model_calls": 0, "tool_calls": 0} for side in ("a", "b")
        }
    )

    @property
    def changed_count(self) -> int:
        count = len(self.env_changes)
        count += sum(1 for p in self.model_call_pairs if p.get("changed"))
        count += sum(1 for p in self.tool_call_pairs if p.get("changed"))
        count += len(self.output_changes)
        return count

    @property
    def added_count(self) -> int:
        count = sum(1 for p in self.model_call_pairs if p.get("added"))
        count += sum(1 for p in self.tool_call_pairs if p.get("added"))
        return count

    @property
    def removed_count(self) -> int:
        count = sum(1 for p in self.model_call_pairs if p.get("removed"))
        count += sum(1 for p in self.tool_call_pairs if p.get("removed"))
        return count

    @property
    def has_changes(self) -> bool:
        """True on ANY structural difference: changed, added or removed.

        This is the ``--assert-no-regressions`` contract ("exits 1 on any change",
        the private design/spec/diff-report-v1.md). ``changed_count`` alone excludes added and
        removed calls, so a gate on it passed when calls appeared or vanished.
        """
        return (self.changed_count + self.added_count + self.removed_count) > 0

    @property
    def malformed_line_count(self) -> int:
        """Total record lines skipped as malformed, both sides, all record files."""
        return sum(
            count for files in self.skipped_malformed_lines.values() for count in files.values()
        )

    @property
    def is_complete(self) -> bool:
        """False when any record line was skipped as malformed.

        ``has_changes`` is computed over the records that parsed. When this is
        False, "no changes" is not established — a skipped line can be the very
        record that pairs with an "added" or "removed" entry on the other side —
        so ``--assert-no-regressions`` exits 2, "cannot compare" (ADR-0303
        Amendment 1), rather than 0 or 1.
        """
        return self.malformed_line_count == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_a_id": self.run_a_id,
            "run_b_id": self.run_b_id,
            "summary": {
                "changed": self.changed_count,
                "added": self.added_count,
                "removed": self.removed_count,
            },
            # The gate's own verdict (ADR-0303): a consumer of the JSON reads the
            # property --assert-no-regressions uses instead of re-deriving it.
            "has_changes": self.has_changes,
            # Always present, zeros included: absent would not distinguish "none
            # skipped" from "an older nova that skipped silently" (ADR-0303 Am. 1).
            "skipped_malformed_lines": {
                side: dict(files) for side, files in self.skipped_malformed_lines.items()
            },
            "sections": {
                "environment": {"changes": self.env_changes},
                "model_calls": {
                    "aligned": len([
                        p for p in self.model_call_pairs
                        if not p.get("added") and not p.get("removed")
                    ]),
                    "changed": sum(1 for p in self.model_call_pairs if p.get("changed")),
                    "added": sum(1 for p in self.model_call_pairs if p.get("added")),
                    "removed": sum(1 for p in self.model_call_pairs if p.get("removed")),
                    "pairs": self.model_call_pairs,
                },
                "tool_calls": {
                    "aligned": len([
                        p for p in self.tool_call_pairs
                        if not p.get("added") and not p.get("removed")
                    ]),
                    "changed": sum(1 for p in self.tool_call_pairs if p.get("changed")),
                    "added": sum(1 for p in self.tool_call_pairs if p.get("added")),
                    "removed": sum(1 for p in self.tool_call_pairs if p.get("removed")),
                    "pairs": self.tool_call_pairs,
                },
                "outputs": {"changes": self.output_changes},
            },
        }

    def write(self, output_path: Path) -> None:
        output_path.write_text(json.dumps(self.as_dict(), indent=2))
