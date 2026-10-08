from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from novafabric.replay._contract import TOOL_SURFACE_MCP, is_interceptable_tool_call
from novafabric.replay._flags import ReplayFlags


@dataclass
class PolicyDecision:
    tool_call_id: str
    tool_name: str
    mutation_class: str
    decision: Literal["mock", "allow", "deny", "live"]
    reason: str


class PolicyEvaluator:
    def __init__(self, replay_policy: dict[str, Any], flags: ReplayFlags) -> None:
        self._policy = replay_policy
        self._flags = flags
        # tool name -> (decision, reason). The schema shape is
        # `{tool_name, allow: bool}`; the legacy `action: replay|refuse` shape is
        # still read so replay.yaml files written against it keep working.
        self._tool_overrides: dict[str, tuple[Literal["mock", "allow", "deny"], str]] = {}
        for override in replay_policy.get("tool_overrides", []):
            name = override.get("tool_name", "")
            if not name:
                continue
            allow = override.get("allow")
            action = override.get("action", "")
            if isinstance(allow, bool):
                self._tool_overrides[name] = (
                    "allow" if allow else "deny",
                    f"tool_override: allow={'true' if allow else 'false'}",
                )
            elif action:
                self._tool_overrides[name] = (
                    "allow" if action == "replay" else
                    "deny" if action == "refuse" else
                    "mock",
                    f"tool_override: {action}",
                )

    def check_tool(self, tool_call: dict[str, Any]) -> PolicyDecision:
        tool_call_id = tool_call.get("tool_call_id", "")
        tool_name = tool_call.get("tool_name", "")
        mutation_class = tool_call.get("mutation_class", "unknown")

        # Per-tool override takes precedence
        if tool_name in self._tool_overrides:
            decision, reason = self._tool_overrides[tool_name]
            return PolicyDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                mutation_class=mutation_class,
                decision=decision,
                reason=reason,
            )

        # Mocked mode (ADR-0300): only the intercepted surface is served from
        # the capsule. Anything else is not controlled by replay and runs live;
        # saying "served from cache" for it was a false statement.
        if self._flags.mode == "mocked":
            if is_interceptable_tool_call(tool_call):
                return PolicyDecision(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    mutation_class=mutation_class,
                    decision="mock",
                    reason=(
                        f"mocked mode: served from the capsule via {TOOL_SURFACE_MCP}; "
                        "an unmatched call is refused, never run live"
                    ),
                )
            transport = tool_call.get("transport", "unknown")
            return PolicyDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                mutation_class=mutation_class,
                decision="live",
                reason=(
                    f"mocked mode: transport={transport!r} is not intercepted by "
                    "replay; the tool runs live"
                ),
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
            icon = {"mock": "MOCK", "allow": "ALLOW", "live": "LIVE"}.get(d.decision, "DENY")
            lines.append(
                f"  [{icon}] {d.tool_name}  mutation_class={d.mutation_class}  ({d.reason})\n"
            )
        return "".join(lines)
