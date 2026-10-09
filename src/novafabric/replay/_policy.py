"""Tool policy for replay: ``replay.yaml`` ``tool_overrides`` and the safety ladder.

One implementation, used twice (ADR-0306 D8, slice 2):

* the engine resolves ``replay.yaml`` into a per-tool override table and hands it
  to the replayed process (``TOOL_POLICY_ENV``), where ``MockToolDispatcher``
  applies :func:`decide_intercepted` to every intercepted call;
* ``--dry-run`` renders :meth:`PolicyEvaluator.check_tool`, which calls the same
  :func:`decide_intercepted` for every recorded call.

Trust is asymmetric because ``replay.yaml`` ships inside the capsule (ADR-0306
fact 9, owner decision 2026-10-09 "Strict"): a **restriction** it states
(``allow: false``) always holds, even under ``--permissive``; a **permission**
(``allow: true``) takes effect only when the operator also passes the ladder flag
for the tool's mutation class. The class that gates an MCP call is always
``unknown`` -- the class a capsule records is never used to grant anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from novafabric.replay._contract import TOOL_SURFACE_MCP, not_servable_reason, tool_surface
from novafabric.replay._flags import ReplayFlags, ladder_flag

#: Env var naming the JSON override table the replayed process enforces.
TOOL_POLICY_ENV = "NOVAFABRIC_REPLAY_TOOL_POLICY_PATH"

#: Env var set to ``1`` when the replayed process MUST install its dispatchers
#: even under ``--permissive``: an ``allow: false`` override cannot hold if the
#: tool dispatcher is missing, so a failed install stops the process.
INSTALL_REQUIRED_ENV = "NOVAFABRIC_REPLAY_INSTALL_REQUIRED"

#: What a dispatcher does with one intercepted call.
ToolAction = Literal["serve", "live", "refuse"]


@dataclass(frozen=True)
class ToolOverride:
    """One ``replay.yaml`` ``tool_overrides`` entry, normalised."""

    tool_name: str
    allow: bool
    rationale: str | None = None

    @property
    def label(self) -> str:
        return f"tool_override allow={'true' if self.allow else 'false'}"


def load_tool_overrides(replay_policy: dict[str, Any]) -> dict[str, ToolOverride]:
    """``tool_name`` -> override. The schema shape is ``{tool_name, allow: bool}``;
    the legacy ``action: replay|refuse`` shape is still read. An entry with
    neither (or another ``action``) is ignored rather than guessed at."""
    out: dict[str, ToolOverride] = {}
    for override in replay_policy.get("tool_overrides") or []:
        if not isinstance(override, dict):
            continue
        name = override.get("tool_name", "")
        if not isinstance(name, str) or not name:
            continue
        allow = override.get("allow")
        action = override.get("action", "")
        rationale = override.get("rationale")
        rationale = str(rationale) if rationale else None
        if isinstance(allow, bool):
            out[name] = ToolOverride(name, allow, rationale)
        elif action in ("replay", "refuse"):
            out[name] = ToolOverride(name, action == "replay", rationale)
    return out


def gating_mutation_class(surface: str | None, mutation_class: str) -> str:
    """The class the ladder is checked against. An MCP call is always gated as
    ``unknown`` (the hook and the proxy record nothing better, and a capsule's
    recorded class must not grant a permission); a ``record.tool`` call uses the
    class declared in the workload's own code."""
    if surface == TOOL_SURFACE_MCP:
        return "unknown"
    return mutation_class or "unknown"


def decide_intercepted(
    *,
    override: bool | None,
    servable_match: bool,
    permitted: bool,
    permissive: bool,
) -> ToolAction:
    """What happens to one call on an intercepted surface (ADR-0306 D7/D8).

    * ``allow: true`` **and** the operator's ladder flag -> run live (the record,
      if one matches, is consumed but not served);
    * a matched, servable record -> served (``allow: false`` included: serving
      is not re-execution);
    * otherwise the call is a divergence: ``allow: false`` refuses it, even under
      ``--permissive``; strict refuses it; ``--permissive`` runs it live only
      when the ladder flag permits its class.
    """
    if override is True and permitted:
        return "live"
    if servable_match:
        return "serve"
    if override is False or not permissive:
        return "refuse"
    return "live" if permitted else "refuse"


@dataclass
class PolicyDecision:
    tool_call_id: str
    tool_name: str
    mutation_class: str
    decision: Literal["mock", "allow", "deny", "live"]
    reason: str
    #: Extra words printed after the label in the dry-run report.
    label_note: str = ""


class PolicyEvaluator:
    def __init__(self, replay_policy: dict[str, Any], flags: ReplayFlags) -> None:
        self._policy = replay_policy
        self._flags = flags
        self._overrides = load_tool_overrides(replay_policy)

    @property
    def overrides(self) -> dict[str, ToolOverride]:
        return dict(self._overrides)

    # ── what the replayed process enforces ──────────────────────────────────

    def tool_policy_table(self) -> dict[str, Any]:
        """The table written to ``TOOL_POLICY_ENV`` -- tool name -> ``allow``.
        The ladder travels separately (``TOOL_LADDER_ENV``), from the command line."""
        return {"overrides": {n: {"allow": o.allow} for n, o in sorted(self._overrides.items())}}

    def has_deny_override(self) -> bool:
        return any(not o.allow for o in self._overrides.values())

    def unenforceable_overrides(
        self, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """``allow: false`` overrides naming a tool the capsule recorded on a
        transport replay cannot intercept (not MCP ``tools/call``, not
        ``record.tool``): replay cannot stop it running live (ADR-0306 Q5)."""
        out: list[dict[str, Any]] = []
        for name, override in sorted(self._overrides.items()):
            if override.allow:
                continue
            transports = sorted({
                str(tc.get("transport", "unknown"))
                for tc in tool_calls
                if tc.get("tool_name") == name and tool_surface(tc) is None
            })
            if transports:
                out.append({"tool_name": name, "transports": transports})
        return out

    def override_report(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``replay_contract.tool_overrides`` (ADR-0306 D11): one entry per
        override, saying whether this replay honours it and why."""
        out: list[dict[str, Any]] = []
        for name, override in sorted(self._overrides.items()):
            recorded = [tc for tc in tool_calls if tc.get("tool_name") == name]
            entry: dict[str, Any] = {
                "tool_name": name, "decision": "allow" if override.allow else "deny",
            }
            if not recorded:
                entry.update(honoured=False, reason=(
                    "override_unused: the capsule records no call to this tool; "
                    "an intercepted call to it is still governed by the override"
                ))
            elif any(tool_surface(tc) is None for tc in recorded):
                transports = sorted({
                    str(tc.get("transport", "unknown"))
                    for tc in recorded if tool_surface(tc) is None
                })
                if override.allow:
                    entry.update(honoured=True, reason=(
                        f"transport {', '.join(transports)} is not intercepted: the "
                        "tool runs live with or without this override"
                    ))
                else:
                    entry.update(honoured=False, reason=(
                        f"override_unenforceable: transport {', '.join(transports)} is "
                        "not intercepted, so replay cannot stop the tool running live"
                    ))
            elif not override.allow:
                entry.update(honoured=True, reason=(
                    "served from the capsule or refused; never run live"
                ))
            else:
                classes = sorted({
                    gating_mutation_class(
                        tool_surface(tc), str(tc.get("mutation_class") or "unknown")
                    )
                    for tc in recorded
                })
                missing = [c for c in classes if not self._flags.permits(c)]
                if missing:
                    flags = sorted({ladder_flag(c) for c in missing})
                    entry.update(honoured=False, reason=(
                        "override_not_honoured: a permission from the capsule needs "
                        f"the operator's {' / '.join(flags)}; the recorded result is "
                        "served instead"
                    ))
                else:
                    entry.update(honoured=True, reason=(
                        "re-executed live: the operator's ladder flag permits "
                        f"mutation_class {', '.join(classes)}"
                    ))
            if override.rationale:
                entry["rationale"] = override.rationale
            out.append(entry)
        return out

    # ── what --dry-run prints ───────────────────────────────────────────────

    def check_tool(self, tool_call: dict[str, Any]) -> PolicyDecision:
        tool_call_id = tool_call.get("tool_call_id", "")
        tool_name = tool_call.get("tool_name", "")
        mutation_class = tool_call.get("mutation_class", "unknown")

        def decided(
            decision: Literal["mock", "allow", "deny", "live"], reason: str, note: str = ""
        ) -> PolicyDecision:
            return PolicyDecision(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                mutation_class=mutation_class,
                decision=decision,
                reason=reason,
                label_note=note,
            )

        override = self._overrides.get(tool_name)

        # Mocked mode (ADR-0300, ADR-0306): the same decision the replayed
        # process makes (``decide_intercepted``), for the recorded call.
        if self._flags.mode == "mocked":
            return self._check_mocked(tool_call, override, decided)

        # Forensic mode: nothing is allowed to execute
        if self._flags.mode == "forensic":
            return decided("mock", "forensic mode: read-only inspection")

        # Other modes install no tool dispatcher; the legacy table is kept.
        if override is not None:
            return decided(
                "allow" if override.allow else "deny",
                f"tool_override: allow={'true' if override.allow else 'false'}",
            )

        # Safety ladder check
        if self._flags.permits(mutation_class):
            return decided("allow", "permitted by safety flags")

        return decided(
            "deny", f"mutation_class={mutation_class!r} not permitted by current flags"
        )

    def _check_mocked(
        self,
        tool_call: dict[str, Any],
        override: ToolOverride | None,
        decided: Callable[..., PolicyDecision],
    ) -> PolicyDecision:
        surface = tool_surface(tool_call)
        if surface is None:
            transport = tool_call.get("transport", "unknown")
            base = f"mocked mode: transport={transport!r} is not intercepted by replay"
            if override is not None and not override.allow:
                if not self._flags.permissive:
                    return decided("deny", (
                        f"{base}; {override.label} cannot be enforced, so a strict "
                        "replay refuses to start (ToolOverrideUnenforceable)"
                    ), "replay refuses to start")
                return decided("live", (
                    f"{base}; {override.label} cannot be enforced: under --permissive "
                    "the tool runs live and override_unenforceable is reported"
                ), "override cannot be enforced")
            if override is not None:
                return decided(
                    "live", f"{base}; the tool runs live ({override.label} changes nothing)"
                )
            return decided("live", f"{base}; the tool runs live")

        mutation_class = str(tool_call.get("mutation_class") or "unknown")
        gating = gating_mutation_class(surface, mutation_class)
        permitted = self._flags.permits(gating)
        unservable = not_servable_reason(tool_call)
        action = decide_intercepted(
            override=None if override is None else override.allow,
            servable_match=unservable is None,
            permitted=permitted,
            permissive=self._flags.permissive,
        )
        gate = (
            f"gated as mutation_class {gating!r}"
            + (" (MCP calls are always gated as unknown)" if surface == TOOL_SURFACE_MCP else "")
        )
        if action == "serve":
            if override is not None and override.allow:
                needs = ladder_flag(gating)
                return decided("mock", (
                    f"mocked mode: served from the capsule via {surface}; {override.label} "
                    f"is a permission from the capsule and is not honoured without the "
                    f"operator's {needs} ({gate})"
                ), f"override not honoured: needs {needs}")
            if override is not None:
                return decided("mock", (
                    f"mocked mode: served from the capsule via {surface}; {override.label}: "
                    "an unmatched call is refused even under --permissive"
                ), "never live")
            return decided("mock", (
                f"mocked mode: served from the capsule via {surface}; an unmatched "
                "call is refused"
                + (", or under --permissive runs live only if the ladder flag permits "
                   f"it ({gate})")
            ))
        if action == "live":
            if override is not None and override.allow:
                return decided("live", (
                    f"mocked mode: {override.label} and the operator's "
                    f"{ladder_flag(gating)} permits it: re-executed live via {surface}, "
                    "the recorded result is not served"
                ), "override honoured")
            return decided("live", (
                f"mocked mode: intercepted via {surface} but not servable ({unservable}); "
                f"--permissive and the ladder flag permit it ({gate}): runs live"
            ))
        why = f"not servable ({unservable})" if unservable else "no servable record"
        if override is not None and not override.allow:
            tail = f"{override.label}: refused even under --permissive"
        elif self._flags.permissive:
            tail = (
                f"--permissive, but the ladder does not permit it ({gate}; "
                f"needs {ladder_flag(gating)})"
            )
        else:
            tail = "the call is refused, never run live"
        return decided("deny", f"mocked mode: intercepted via {surface} but {why}; {tail}")

    def check_all(self, tool_calls: list[dict[str, Any]]) -> list[PolicyDecision]:
        return [self.check_tool(tc) for tc in tool_calls]

    def dry_run_report(self, tool_calls: list[dict[str, Any]]) -> str:
        decisions = self.check_all(tool_calls)
        if not decisions:
            return "[dry-run] No tool calls in this capsule.\n"

        lines = ["[dry-run: no execution]\n", "Tool call policy report:\n"]
        for d in decisions:
            icon = {"mock": "MOCK", "allow": "ALLOW", "live": "LIVE"}.get(d.decision, "DENY")
            if d.label_note:
                icon = f"{icon} ({d.label_note})"
            lines.append(
                f"  [{icon}] {d.tool_name}  mutation_class={d.mutation_class}  ({d.reason})\n"
            )
        return "".join(lines)
