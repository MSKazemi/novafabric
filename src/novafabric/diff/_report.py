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

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_a_id": self.run_a_id,
            "run_b_id": self.run_b_id,
            "summary": {
                "changed": self.changed_count,
                "added": self.added_count,
                "removed": self.removed_count,
            },
            "sections": {
                "environment": {"changes": self.env_changes},
                "model_calls": {
                    "aligned": len([
                        p for p in self.model_call_pairs
                        if not p.get("added") and not p.get("removed")
                    ]),
                    "changed": sum(1 for p in self.model_call_pairs if p.get("changed")),
                    "added": self.added_count,
                    "removed": self.removed_count,
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
