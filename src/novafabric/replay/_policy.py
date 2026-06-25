from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from novafabric.replay._flags import ReplayFlags


@dataclass
class PolicyDecision:
    tool_call_id: str
    tool_name: str
    mutation_class: str
    decision: Literal["mock", "allow", "deny"]
    reason: str


class PolicyEvaluator:
    def __init__(self, replay_policy: dict[str, Any], flags: ReplayFlags) -> None:
        self._policy = replay_policy
        self._flags = flags
        self._tool_overrides: dict[str, str] = {}
        for override in replay_policy.get("tool_overrides", []):
            name = override.get("tool_name", "")
            action = override.get("action", "")
            if name and action:
                self._tool_overrides[name] = action

    def check_tool(self, tool_call: dict[str, Any]) -> PolicyDecision:
        tool_call_id = tool_call.get("tool_call_id", "")
        tool_name = tool_call.get("tool_name", "")
        mutation_class = tool_call.get("mutation_class", "unknown")

        # Per-tool override takes precedence
        if tool_name in self._tool_overrides:
            override_action = self._tool_overrides[tool_name]
            decision: Literal["mock", "allow", "deny"] = (
                "allow" if override_action == "replay" else
                "deny" if override_action == "refuse" else
                "mock"
            )
            return PolicyDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                mutation_class=mutation_class,
                decision=decision,
                reason=f"tool_override: {override_action}",
            )

        # In mocked mode, always mock tools regardless of flags
        if self._flags.mode == "mocked":
            return PolicyDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                mutation_class=mutation_class,
                decision="mock",
                reason="mocked mode: all tools served from cache",
            )

        # Forensic mode: nothing is allowed to execute
        if self._flags.mode == "forensic":
            return PolicyDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                mutation_class=mutation_class,
                decision="mock",
                reason="forensic mode: read-only inspection",
            )

        # Safety ladder check
        if self._flags.permits(mutation_class):
            return PolicyDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                mutation_class=mutation_class,
                decision="allow",
                reason="permitted by safety flags",
            )

        return PolicyDecision(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            mutation_class=mutation_class,
            decision="deny",
            reason=f"mutation_class={mutation_class!r} not permitted by current flags",
        )

    def check_all(self, tool_calls: list[dict[str, Any]]) -> list[PolicyDecision]:
        return [self.check_tool(tc) for tc in tool_calls]

    def dry_run_report(self, tool_calls: list[dict[str, Any]]) -> str:
        decisions = self.check_all(tool_calls)
        if not decisions:
            return "[dry-run] No tool calls in this capsule.\n"

        lines = ["[dry-run: no execution]\n", "Tool call policy report:\n"]
        for d in decisions:
            icon = "MOCK" if d.decision == "mock" else "ALLOW" if d.decision == "allow" else "DENY"
            lines.append(
                f"  [{icon}] {d.tool_name}  mutation_class={d.mutation_class}  ({d.reason})\n"
            )
        return "".join(lines)
