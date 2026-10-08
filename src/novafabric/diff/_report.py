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
